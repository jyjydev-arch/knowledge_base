"""启动检查与健康检查

这三条是本轮改造的全部理由，所以每条都钉住一个"错了不会报错、只会静默失真"的点：
  1. 表没建，进程照样启动、`/health` 照样 200 —— 故障被读成正常；
  2. 期望表清单来自 `Base.metadata`，**模型没导入时它是空的**，于是"缺 6 张"被算成"缺 0 张"；
  3. `/health` 是免鉴权接口，连接串里的用户名密码绝不能出现在响应里。
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import startup
from app.core.config import settings

TABLES = {
    "chat_messages", "chat_sessions", "chunks", "documents", "kb_shares",
    "knowledge_bases", "tokens", "users",
}  # 和 app/db/models.py 里的 __tablename__ 保持一致，加表就要同步这里


def fresh_engine(tmp_path, name="probe.db"):
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")


def call(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 期望表清单
# --------------------------------------------------------------------------


def test_期望表清单是六张():
    assert set(startup.expected_tables()) == TABLES


def test_元数据为空时不许判成正常(monkeypatch):
    """Base.metadata 只含"已被导入"的模型；空清单必须报错，不然探库永远是绿的"""
    async def empty():
        return {"ok": False, "dialect": "sqlite", "checked_tables": 0, "missing_tables": [], "error": None}

    monkeypatch.setattr(startup, "expected_tables", lambda: [])
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    info = call(startup.inspect_database(engine))
    assert info["error"] == startup.ERROR_NO_METADATA and info["ok"] is False
    assert empty  # 上面这行只是让 lint 满意，真正断言在上一行


# --------------------------------------------------------------------------
# inspect_database / create_all
# --------------------------------------------------------------------------


def test_空库能查出缺哪几张表(tmp_path):
    info = call(startup.inspect_database(fresh_engine(tmp_path)))
    assert info["ok"] is False
    assert set(info["missing_tables"]) == TABLES
    assert info["error"] == startup.ERROR_MISSING_TABLES


def test_建表后检查通过(tmp_path):
    engine = fresh_engine(tmp_path)
    call(startup.create_all(engine))
    info = call(startup.inspect_database(engine))
    assert info["ok"] is True and info["missing_tables"] == [] and info["error"] is None
    assert info["dialect"] == "sqlite"


def test_探库连不上时不抛异常只给类别():
    engine = create_async_engine("postgresql+asyncpg://postgres:LEAKED-PASSWORD@127.0.0.1:1/nope")
    info = call(startup.inspect_database(engine))
    assert info["ok"] is False and info["error"] == startup.ERROR_CONNECTION
    assert "LEAKED-PASSWORD" not in str(info)  # 结果结构里不能有凭据


def test_内存库判据用_url_database():
    """str(url) 把冒号百分号编码成 %3Amemory%3A，用它判断永远为 False（真踩过）"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    assert ":memory:" not in str(engine.url)
    assert startup._is_ephemeral_engine(engine) is True
    assert engine.url.database == ":memory:"


# --------------------------------------------------------------------------
# health_payload
# --------------------------------------------------------------------------


def test_内存库返回200并标注ephemeral():
    """和启动检查口径一致：内存库没有持久 schema 是定义使然，不是故障"""
    code, data = call(startup.health_payload(create_async_engine("sqlite+aiosqlite:///:memory:")))
    assert code == 200 and data["database"]["ephemeral"] is True


def test_缺表时health给503(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    code, data = call(startup.health_payload(fresh_engine(tmp_path, "health_missing.db")))
    assert code == 503 and data["status"] == "degraded"
    assert set(data["database"]["missing_tables"]) == TABLES


def test_连不上时health给503(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://postgres:pw@127.0.0.1:1/nope")
    code, data = call(
        startup.health_payload(
            create_async_engine("postgresql+asyncpg://postgres:LEAKED-PASSWORD@127.0.0.1:1/nope"), 3.0
        )
    )
    assert code == 503 and data["database"]["error"] == startup.ERROR_CONNECTION
    assert "LEAKED-PASSWORD" not in str(data)


def test_探库超时会返回503而不是挂住(tmp_path, monkeypatch):
    """远端不可达时 TCP 连接挂在内核重试里；没有超时，/health 自己就成故障源

    引擎必须用文件库：内存库会先走 ephemeral 短路直接返回 200，压根到不了超时分支
    （这个坑是写测试时踩到的，所以顺手把顺序也验一遍）。
    """
    engine = fresh_engine(tmp_path, "slow.db")

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.2)
        return {"ok": True, "dialect": "sqlite", "missing_tables": [], "checked_tables": 6, "error": None}

    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'slow.db'}")
    monkeypatch.setattr(startup, "inspect_database", slow)
    code, data = call(startup.health_payload(engine, 0.01))
    assert code == 503 and data["database"]["error"] == startup.ERROR_TIMEOUT


# --------------------------------------------------------------------------
# 启动策略
# --------------------------------------------------------------------------


def test_内存库跳过启动检查(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    result = call(startup.ensure_schema_on_startup(create_async_engine("sqlite+aiosqlite:///:memory:")))
    assert result == {"skipped": True, "ok": True, "created": False, "missing_tables": []}


def test_缺表默认只告警不阻断启动(tmp_path, monkeypatch, caplog):
    """默认 DB_STRICT_STARTUP=False：不改现有行为，但 /health 会如实 503"""
    engine = fresh_engine(tmp_path, "warn.db")
    monkeypatch.setattr(settings, "DATABASE_URL", str(engine.url).replace("%3A", ":"))
    monkeypatch.setattr(settings, "AUTO_CREATE_TABLES", False)
    monkeypatch.setattr(settings, "DB_STRICT_STARTUP", False)
    result = call(startup.ensure_schema_on_startup(engine))
    assert result["ok"] is False and result["error"] == startup.ERROR_MISSING_TABLES
    assert "init_db.py" in caplog.text  # 提示语要能直接照着做


def test_自动建表开关生效(tmp_path, monkeypatch):
    engine = fresh_engine(tmp_path, "auto.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'auto.db'}")
    monkeypatch.setattr(settings, "AUTO_CREATE_TABLES", True)
    result = call(startup.ensure_schema_on_startup(engine))
    assert result["ok"] is True and result["created"] is True
    assert call(startup.inspect_database(engine))["missing_tables"] == []


def test_严格模式下缺表直接拒绝启动(tmp_path, monkeypatch):
    engine = fresh_engine(tmp_path, "strict.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'strict.db'}")
    monkeypatch.setattr(settings, "AUTO_CREATE_TABLES", False)
    monkeypatch.setattr(settings, "DB_STRICT_STARTUP", True)
    with pytest.raises(RuntimeError, match="knowledge_bases"):
        call(startup.ensure_schema_on_startup(engine))


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------


def test_health接口如实反映缺表(client, tmp_path, monkeypatch):
    engine = fresh_engine(tmp_path, "http_missing.db")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'http_missing.db'}")
    monkeypatch.setattr(startup, "get_engine", lambda: engine)
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == 503 and "init_db.py" in body["msg"]
    assert set(body["data"]["database"]["missing_tables"]) == TABLES


def test_health接口不泄露连接信息(client, tmp_path, monkeypatch):
    engine = create_async_engine("postgresql+asyncpg://postgres:LEAKED-PASSWORD@127.0.0.1:1/nope")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://postgres:LEAKED-PASSWORD@127.0.0.1:1/nope")
    monkeypatch.setattr(startup, "get_engine", lambda: engine)
    text = client.get("/health").text
    assert "LEAKED-PASSWORD" not in text and "://" not in text


def test_默认车道health是200且带内存库标注(client):
    body = client.get("/health").json()
    assert body["status"] == 200
    assert body["data"]["database"]["ephemeral"] is True


# --------------------------------------------------------------------------
# 防呆：前缀
# --------------------------------------------------------------------------


def test_所有router的prefix都不许带版本号():
    """版本前缀只在 main.py 出现一次。写进 router 里会叠成 /api/v1/api/v1/...（实测踩过）"""
    from app.api.v1.aichatrouter import router as aichat_router
    from app.api.v1.documentrouter import router as document_router
    from app.api.v1.kbrouter import router as kb_router
    from app.api.v1.userrouter import router as user_router

    for name, router in (
        ("user", user_router),
        ("document", document_router),
        ("kb", kb_router),
        ("aichat", aichat_router),  # 还没 include，也要一起钉住
    ):
        assert not router.prefix.lower().startswith("/api"), f"{name} 的 prefix 写成了 {router.prefix}"


def test_注册出来的路径没有双前缀():
    from app.main import app

    bad = [path for path in app.openapi()["paths"] if "/api/v1/api" in path]
    assert bad == []


def test_aichat对话路由已接入且前缀正确():
    """接线完成（M5）：8 条都在 /api/v1/aichat 下面，一条不多一条不少

    这条测试是双前缀事故的延续：include 时忘了传 prefix 会全 404，
    prefix 写在 router 里又会叠成 /api/v1/api/v1/aichat，两种都要在这里挡住。
    """
    from app.main import app

    paths = set(app.openapi()["paths"])
    expected = {
        "/api/v1/aichat/conversations",
        "/api/v1/aichat/conversations/{conversation_id}",
        "/api/v1/aichat/conversations/{conversation_id}/messages",
        "/api/v1/aichat/conversations/{conversation_id}/chat",
        "/api/v1/aichat/conversations/{conversation_id}/stop",
        "/api/v1/aichat/agents",
    }
    chat_paths = {path for path in paths if "/aichat" in path}
    assert chat_paths == expected
    # 6 个路径 8 个操作（conversations 有 POST/GET，conversation_id 有 PUT/DELETE）
    operations = sum(len(app.openapi()["paths"][path]) for path in expected)
    assert operations == 8, operations
