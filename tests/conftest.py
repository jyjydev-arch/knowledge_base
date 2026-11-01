"""pytest 公共 fixture

测试不依赖真实的 PostgreSQL：用临时文件版 SQLite 跑完整流程。
（users / documents 的主键在 Python 端就会生成 uuid，见 app/db/models.py）
"""

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAMES", "admin")
os.environ.setdefault("SQL_ECHO", "False")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db import models  # noqa: F401,E402
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


@pytest.fixture()
def db_url(tmp_path) -> str:
    """每个用例一个独立的库文件"""
    return f"sqlite+aiosqlite:///{tmp_path / 'kb_test.db'}"


@pytest.fixture()
def client(db_url):
    _create_tables(db_url)
    engine = create_async_engine(db_url, poolclass=NullPool)
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def override_get_db():
        async with SessionLocal() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    from app.main import app

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


@pytest.fixture()
def register(client):
    """注册工具：返回响应对象"""

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


def _auth_headers(client, username: str, email: str, password: str = "abc123456") -> dict:
    client.post(
        "/api/v1/user/register",
        json={"username": username, "email": email, "password": password},
    )
    resp = client.post("/api/v1/user/login", json={"account": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture()
def user_headers(client) -> dict:
    """普通用户 zhangsan 的登录态"""
    return _auth_headers(client, "zhangsan", "zhangsan@example.com")


@pytest.fixture()
def admin_headers(client) -> dict:
    """管理员 admin 的登录态（ADMIN_USERNAMES=admin）"""
    return _auth_headers(client, "admin", "admin@example.com")


@pytest.fixture()
def other_headers(client) -> dict:
    """另一个普通用户 lisi 的登录态"""
    return _auth_headers(client, "lisi", "lisi@example.com")
