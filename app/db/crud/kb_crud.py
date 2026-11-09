"""知识库与共享授权的 CRUD

只读写数据库、不抛 HTTP 异常；权限判定规则在 app/services/kb_access.py，
这样「谁能做什么」只有一处定义，路由和测试都复用同一套。
"""

import uuid as uuid_lib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import KbPermission, KbShare, KnowledgeBase


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def get_kb(kb_id, db: AsyncSession) -> KnowledgeBase | None:
    """按 id 取知识库本体，**不带 owner 过滤**

    共享之后「不是我的库」也可能是我能读的库，所以这里只负责取，
    能不能访问交给 kb_access.resolve() 判。非法 id 返回 None。
    """
    pk = _to_uuid(kb_id)
    if pk is None:
        return None
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == pk, KnowledgeBase.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def list_owned_kbs(owner_id, db: AsyncSession, offset: int = 0, limit: int = 20) -> tuple[list[KnowledgeBase], int]:
    """某用户自己创建的库（M0 的 /kb/list 再用，先备好）"""
    from sqlalchemy import func

    uid = _to_uuid(owner_id)
    if uid is None:
        return [], 0
    conditions = [KnowledgeBase.owner_id == uid, KnowledgeBase.deleted_at.is_(None)]
    total = (await db.execute(select(func.count()).select_from(KnowledgeBase).where(*conditions))).scalar_one()
    result = await db.execute(
        select(KnowledgeBase).where(*conditions).order_by(KnowledgeBase.updated_at.desc()).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), int(total)


async def get_share(kb_id, grantee_id, db: AsyncSession) -> KbShare | None:
    pk, uid = _to_uuid(kb_id), _to_uuid(grantee_id)
    if pk is None or uid is None:
        return None
    result = await db.execute(
        select(KbShare).where(KbShare.kb_id == pk, KbShare.grantee_id == uid)
    )
    return result.scalar_one_or_none()


async def grant_share(
    kb_id, grantee_id, granted_by, db: AsyncSession, permission: str = KbPermission.READ
) -> KbShare | None:
    """授权（幂等）：已有记录就更新权限，没有就插入

    返回 None 表示失败（id 非法 / 被授权用户或库不存在触发外键 / 唯一索引冲突且更新失败）。
    """
    pk, uid, giver = _to_uuid(kb_id), _to_uuid(grantee_id), _to_uuid(granted_by)
    if pk is None or uid is None:
        return None

    existing = await get_share(pk, uid, db)
    if existing is not None:
        existing.permission = permission
        await db.commit()
        await db.refresh(existing)
        return existing

    record = KbShare(kb_id=pk, grantee_id=uid, granted_by=giver, permission=permission)
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await get_share(pk, uid, db)
    await db.refresh(record)
    return record


async def revoke_share(kb_id, grantee_id, db: AsyncSession) -> bool:
    pk, uid = _to_uuid(kb_id), _to_uuid(grantee_id)
    if pk is None or uid is None:
        return False
    result = await db.execute(select(KbShare).where(KbShare.kb_id == pk, KbShare.grantee_id == uid))
    record = result.scalar_one_or_none()
    if record is None:
        return False
    await db.delete(record)
    await db.commit()
    return True


async def list_shares(kb_id, db: AsyncSession) -> list[KbShare]:
    """某库的授权名单（带被授权人信息，用 selectinload 避免异步下懒加载炸）"""
    pk = _to_uuid(kb_id)
    if pk is None:
        return []
    result = await db.execute(
        select(KbShare)
        .where(KbShare.kb_id == pk)
        .options(selectinload(KbShare.grantee))
        .order_by(KbShare.created_at.desc())
    )
    return list(result.scalars().all())


async def list_shared_kb_ids(grantee_id, db: AsyncSession) -> list[uuid_lib.UUID]:
    """被授权给这个用户的知识库 id（不含 public 库，也不含自己的库 —— 由调用方合并）"""
    uid = _to_uuid(grantee_id)
    if uid is None:
        return []
    result = await db.execute(
        select(KbShare.kb_id)
        .join(KnowledgeBase, KnowledgeBase.id == KbShare.kb_id)
        .where(KbShare.grantee_id == uid, KnowledgeBase.deleted_at.is_(None))
        .order_by(KbShare.created_at.desc())
    )
    return list(result.scalars().all())


async def list_public_kb_ids(db: AsyncSession) -> list[uuid_lib.UUID]:
    """全站可见（public）的库 id"""
    result = await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.visibility == "public", KnowledgeBase.deleted_at.is_(None)
        )
    )
    return list(result.scalars().all())
