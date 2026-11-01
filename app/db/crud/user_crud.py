"""用户表的 CRUD

约定:
    1. CRUD 层只负责读写数据库，**不抛 HTTP 异常**，查不到就返回 None，
       由路由层决定返回什么 status / msg；
    2. 入参顺序统一为 (业务参数..., db)，db 永远放最后一个。
"""

import uuid as uuid_lib
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.schemas.user_schemas import UserCreate, UserUpdate


def _to_uuid(raw) -> uuid_lib.UUID | None:
    """把 str/int 等转成 UUID，非法就返回 None（避免拼进 SQL 后报 500）"""
    if raw is None:
        return None
    if isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


async def get_user_by_id(user_id, db: AsyncSession) -> User | None:
    pk = _to_uuid(user_id)
    if pk is None:
        return None
    result = await db.execute(select(User).where(User.id == pk))
    return result.scalar_one_or_none()


async def get_user_by_username(username: str, db: AsyncSession) -> User | None:
    if not username:
        return None
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_email(email: str, db: AsyncSession) -> User | None:
    if not email:
        return None
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_account(account: str, db: AsyncSession) -> User | None:
    """登录用：账号既可以是用户名也可以是邮箱"""
    if not account:
        return None
    result = await db.execute(
        select(User).where(or_(User.username == account, User.email == account))
    )
    return result.scalars().first()


async def get_user_by_id_for_update(user_id, db: AsyncSession) -> User | None:
    """加行锁的查询，改密码 / 注销这类敏感操作可以用"""
    pk = _to_uuid(user_id)
    if pk is None:
        return None
    result = await db.execute(select(User).where(User.id == pk).with_for_update())
    return result.scalar_one_or_none()


async def is_username_taken(username: str, db: AsyncSession, exclude_id=None) -> bool:
    stmt = select(User.id).where(User.username == username)
    pk = _to_uuid(exclude_id)
    if pk is not None:
        stmt = stmt.where(User.id != pk)
    return (await db.execute(stmt)).first() is not None


async def is_email_taken(email: str, db: AsyncSession, exclude_id=None) -> bool:
    stmt = select(User.id).where(User.email == email)
    pk = _to_uuid(exclude_id)
    if pk is not None:
        stmt = stmt.where(User.id != pk)
    return (await db.execute(stmt)).first() is not None


async def create_user(user_in: UserCreate, hashed_password: str, db: AsyncSession) -> User:
    """创建用户

    用户名 / 邮箱唯一索引冲突时返回 None（并回滚事务），
    由路由层给出「已被注册」的提示。
    """
    new_user = User(
        username=user_in.username,
        email=user_in.email,
        hashed_password=hashed_password,
    )
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    await db.refresh(new_user)
    return new_user


async def update_user(user: User, user_in: UserUpdate, db: AsyncSession) -> User | None:
    """更新用户名 / 邮箱，只改前端真正传上来的字段"""
    changes = user_in.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        return user

    for field, value in changes.items():
        setattr(user, field, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    await db.refresh(user)
    return user


async def set_password(user: User, hashed_password: str, db: AsyncSession) -> User:
    user.hashed_password = hashed_password
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(user: User, db: AsyncSession) -> bool:
    """删除用户（token / documents 由外键 ON DELETE CASCADE 一起清掉）"""
    await db.delete(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def list_users(db: AsyncSession, offset: int = 0, limit: int = 20) -> tuple[Sequence[User], int]:
    """分页查询用户，返回 (当前页数据, 总数)"""
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    result = await db.execute(
        select(User).order_by(User.created_at.desc(), User.username).offset(offset).limit(limit)
    )
    return result.scalars().all(), int(total)
