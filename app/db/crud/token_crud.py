"""token 表的 CRUD

JWT 本身是无状态的，这里把签发出去的 token 落库，
主要是为了能实现「退出登录」和「踢下线」：
    - 登录成功 -> save_token()
    - 退出登录 -> revoke_token()（删掉这一条）
    - 每次带 token 的请求 -> is_token_active() 校验是否还在库里且没过期
"""

import uuid as uuid_lib
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Token


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def save_token(token: str, user_id, expired_at: datetime, db: AsyncSession) -> Token | None:
    """记录一个已签发的 token；重复签发（同一秒内）时用 merge 覆盖"""
    pk = _to_uuid(user_id)
    if pk is None:
        return None
    record = Token(token=token, user_id=pk, expired_at=expired_at)
    try:
        await db.merge(record)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    return record


async def get_token(token: str, db: AsyncSession) -> Token | None:
    if not token:
        return None
    result = await db.execute(select(Token).where(Token.token == token))
    return result.scalar_one_or_none()


async def is_token_active(token: str, db: AsyncSession) -> bool:
    """token 是否仍然有效：在库中 且 未过期"""
    record = await get_token(token, db)
    if record is None:
        return False
    expired_at = record.expired_at
    if expired_at.tzinfo is None:
        expired_at = expired_at.replace(tzinfo=timezone.utc)
    return expired_at > datetime.now(timezone.utc)


async def revoke_token(token: str, db: AsyncSession) -> bool:
    """退出登录：删除该 token"""
    if not token:
        return False
    result = await db.execute(delete(Token).where(Token.token == token))
    await db.commit()
    return result.rowcount > 0


async def revoke_all_user_tokens(user_id, db: AsyncSession) -> int:
    """删除某个用户的全部 token（改密码 / 注销时调用，强制所有端下线）"""
    pk = _to_uuid(user_id)
    if pk is None:
        return 0
    result = await db.execute(delete(Token).where(Token.user_id == pk))
    await db.commit()
    return result.rowcount or 0


async def purge_expired_tokens(db: AsyncSession) -> int:
    """清理已过期的 token，可以挂到定时任务里"""
    result = await db.execute(
        delete(Token).where(Token.expired_at < datetime.now(timezone.utc))
    )
    await db.commit()
    return result.rowcount or 0
