"""pytest 公共 fixture

测试不依赖真实的 PostgreSQL：每个用例一个临时文件 SQLite。
（主键在 Python 端就生成 uuid，见 app/db/models.py，所以换库不用改代码）
"""

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAMES", "admin")
os.environ.setdefault("SQL_ECHO", "False")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db import models  # noqa: F401,E402  建表前必须导入，否则表不在 metadata 里
from app.db.base import Base  # noqa: E402
from app.db.psql_config import get_db  # noqa: E402


def _create_tables(url: str) -> None:
    """用独立的事件循环建表，避免连接跨 loop 复用"""
    ddl_engine = create_async_engine(url, poolclass=NullPool)

    async def _run() -> None:
        async with ddl_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_run())
    asyncio.run(ddl_engine.dispose())


def _make_session_maker(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture()
def db_url(tmp_path) -> str:
    """每个用例一个独立的库文件"""
    return f"sqlite+aiosqlite:///{tmp_path / 'kb_test.db'}"


@pytest.fixture()
def engine(db_url):
    _create_tables(db_url)
    eng = create_async_engine(db_url, poolclass=NullPool)

    # SQLite 默认不启用外键，级联删除/约束相关用例必须打开它
    @event.listens_for(eng.sync_engine, "connect")
    def _fk_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield eng
    asyncio.run(eng.dispose())


@pytest.fixture()
def run(engine):
    """在 ORM 上直接跑一段异步代码，用来造种子数据：

        doc = run(lambda session: make_document(session, kb_id=..., owner_id=...))
    """
    maker = _make_session_maker(engine)

    def _runner(fn):
        async def _inner():
            async with maker() as session:
                return await fn(session)

        return asyncio.run(_inner())

    return _runner


@pytest.fixture()
def client(engine):
    from app.main import app

    maker = _make_session_maker(engine)

    async def override_get_db():
        async with maker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------- 注册 / 登录相关夹具 ----------------
@pytest.fixture()
def register(client):
    def _register(username: str = "zhangsan", email: str = "zhangsan@example.com", password: str = "abc123456"):
        return client.post(
            "/api/v1/user/register",
            json={"username": username, "email": email, "password": password},
        )

    return _register


@pytest.fixture()
def login(client):
    def _login(account: str, password: str = "abc123456"):
        return client.post("/api/v1/user/login", json={"account": account, "password": password})

    return _login


def _login_headers(client, username: str, password: str = "abc123456") -> dict:
    resp = client.post("/api/v1/user/login", json={"account": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture()
def user_headers(client, register) -> dict:
    """普通用户 zhangsan 的登录态"""
    register(username="zhangsan", email="zhangsan@example.com")
    return _login_headers(client, "zhangsan")


@pytest.fixture()
def admin_headers(client, register) -> dict:
    """管理员登录态（用户名命中 ADMIN_USERNAMES，注册时 role 直接给 admin）"""
    register(username="admin", email="admin@example.com")
    return _login_headers(client, "admin")


@pytest.fixture()
def other_headers(client, register) -> dict:
    """另一个普通用户 lisi 的登录态"""
    register(username="lisi", email="lisi@example.com")
    return _login_headers(client, "lisi")


@pytest.fixture()
def logged_in(client, register) -> dict:
    """注册 + 登录 zhangsan，返回 {"headers":..., "user_id":...}，供需要种子数据的用例使用"""
    register(username="zhangsan", email="zhangsan@example.com")
    data = client.post("/api/v1/user/login", json={"account": "zhangsan", "password": "abc123456"}).json()["data"]
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "user_id": data["user"]["id"],
    }
