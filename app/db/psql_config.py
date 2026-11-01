# app/db/psql_config.py
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db import models  # noqa: F401  # 保证建表时所有模型都已注册
from app.db.base import Base


def _engine_kwargs(url: str) -> dict:
    """连接池参数

    pool_size / max_overflow 只有连接池型的驱动支持，
    sqlite（主要给测试用）传了会直接报错，所以按 url 区分。
    """
    kwargs: dict = {"echo": settings.SQL_ECHO, "pool_pre_ping": True}
    if not url.startswith("sqlite"):
        kwargs["pool_size"] = settings.DB_POOL_SIZE  # 连接池默认保持的连接数
        kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW  # 允许超出的最大连接数
    return kwargs


# 1. 全局创建一次 Engine（包含连接池）
#    比如: postgresql+asyncpg://postgres:postgres@localhost:5432/vector
engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs(settings.DATABASE_URL))

# 2. 全局创建 Session 工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# 3. 依赖注入函数：只负责给每次请求发一个 Session，并在结束后关闭它
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# 4. 建表（开发调试用；正式环境建议用 alembic 迁移，建表语句见项目根目录 init_db.sql）
async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
