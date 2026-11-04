"""token 表的 CRUD

JWT 本身无状态，这里把签发出去的凭证落库，是为了能「退出登录 / 踢下线 / 改密后强制重登」。

存的不是 JWT 原文，而是 sha256(token)（见 models.py:Token）：
    库被拖走也拿不到能直接用的凭证；校验时把请求带上来的 token 哈希后再查。
"""

import hashlib
import uuid as uuid_lib
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Token


def hash_token(token: str) -> str:
    """token 原文 -> 库里的 token_hash"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def save_token(
    token: str,
    user_id,
    expires_at: datetime,
    db: AsyncSession,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> Token | None:
    """记录一次签发；同一秒重复登录拿到相同 JWT 时，直接返回已有记录"""
    pk = _to_uuid(user_id)
    if pk is None:
        return None

    record = Token(
        token_hash=hash_token(token),
        user_id=pk,
        expires_at=expires_at,
        client_ip=client_ip,
        user_agent=user_agent[:256] if user_agent else None,
    )
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await get_token(token, db)
    await db.refresh(record)
    return record


async def get_token(token: str, db: AsyncSession) -> Token | None:
    if not token:
        return None
    result = await db.execute(select(Token).where(Token.token_hash == hash_token(token)))
    return result.scalar_one_or_none()


async def is_token_active(token: str, db: AsyncSession) -> bool:
    """token 是否仍然有效：在库中（没被登出）且未过期"""
    record = await get_token(token, db)
    if record is None:
        return False

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)


async def revoke_token(token: str, db: AsyncSession) -> bool:
    """退出登录：删掉这一条"""
    if not token:
        return False
    result = await db.execute(delete(Token).where(Token.token_hash == hash_token(token)))
    await db.commit()
    return (result.rowcount or 0) > 0


async def revoke_all_user_tokens(user_id, db: AsyncSession) -> int:
    """删除某用户全部 token（改密码 / 改邮箱时调用，强制所有端重新登录）"""
    pk = _to_uuid(user_id)
    if pk is None:
        return 0
    result = await db.execute(delete(Token).where(Token.user_id == pk))
    await db.commit()
    return result.rowcount or 0


async def purge_expired_tokens(db: AsyncSession) -> int:
    """清理过期 token，建议挂定时任务"""
    result = await db.execute(
        delete(Token).where(Token.expires_at < datetime.now(timezone.utc))
    )
    await db.commit()
    return result.rowcount or 0


async def list_active_tokens(user_id, db: AsyncSession) -> list[Token]:
    """「登录设备」列表用（该接口还没做，先给 CRUD 备好）"""
    pk = _to_uuid(user_id)
    if pk is None:
        return []
    result = await db.execute(
        select(Token)
        .where(Token.user_id == pk, Token.expires_at > datetime.now(timezone.utc))
        .order_by(Token.created_at.desc())
    )
    return list(result.scalars().all())
