"""文档表的 CRUD

约定（见 DEVELOPMENT.md 第 5 节）:
    - 业务参数在前，db 永远最后一个；
    - 只读写数据库，不抛 HTTP 异常，查不到返回 None；
    - 所有查询都带 owner_id，越权在 CRUD 层就挡掉，不依赖上层记得判断。
"""

import uuid as uuid_lib
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, DocStatus, Document, KnowledgeBase


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def list_documents(
    owner_id,
    db: AsyncSession,
    offset: int = 0,
    limit: int = 20,
    kb_id=None,
    status: str | None = None,
) -> tuple[Sequence[Document], int]:
    """按时间倒序分页取当前用户的文档，返回 (数据, 总数)"""
    uid = _to_uuid(owner_id)
    if uid is None:
        return [], 0

    conditions = [Document.owner_id == uid, Document.deleted_at.is_(None)]
    if kb_id is not None:
        pk = _to_uuid(kb_id)
        if pk is None:
            return [], 0  # kb_id 传了但非法：当作「没有匹配」，不能退化成不过滤
        conditions.append(Document.kb_id == pk)
    if status:
        conditions.append(Document.status == status)

    total = (
        await db.execute(select(func.count()).select_from(Document).where(*conditions))
    ).scalar_one()
    result = await db.execute(
        select(Document)
        .where(*conditions)
        .order_by(Document.created_at.desc(), Document.title)
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all(), int(total)


async def get_document(document_id, owner_id, db: AsyncSession) -> Document | None:
    """取单个文档，owner 不匹配就当不存在（避免探测别人的文档 id）"""
    did, uid = _to_uuid(document_id), _to_uuid(owner_id)
    if did is None or uid is None:
        return None
    result = await db.execute(
        select(Document).where(Document.id == did, Document.owner_id == uid, Document.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def get_knowledge_base(kb_id, owner_id, db: AsyncSession) -> KnowledgeBase | None:
    """上传文档前必须先过这一关：知识库得存在、没被删、而且是当前用户自己的

    少了这一步就能把文件塞进别人的知识库（kb_id 是客户端传进来的）。
    """
    pk, uid = _to_uuid(kb_id), _to_uuid(owner_id)
    if pk is None or uid is None:
        return None
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == pk, KnowledgeBase.owner_id == uid, KnowledgeBase.deleted_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


async def get_document_by_hash(kb_id, file_hash: str, db: AsyncSession) -> Document | None:
    """上传判重：同一个知识库里同一个文件（sha256）不重复入库"""
    pk = _to_uuid(kb_id)
    if pk is None or not file_hash:
        return None
    result = await db.execute(
        select(Document).where(Document.kb_id == pk, Document.file_hash == file_hash)
    )
    return result.scalar_one_or_none()


async def create_document(
    kb_id,
    owner_id,
    title: str,
    filename: str,
    file_path: str,
    file_size: int,
    mime_type: str | None,
    file_hash: str,
    db: AsyncSession,
    *,
    document_id=None,
    status: str = DocStatus.PENDING,
) -> Document | None:
    """新建文档记录，状态默认 pending（等解析/切块任务去推进）

    kb 不存在（外键失败）或同库同文件已存在（唯一索引冲突）都回滚并返回 None。

    document_id 可以由调用方预先指定 —— 上传就是这种场景：
    文件名要用 document_id 拼（data/{user_id}/{kb_id}/{document_id}.{ext}），所以必须先生成 id 再落盘。
    """
    if _to_uuid(kb_id) is None or _to_uuid(owner_id) is None:
        return None

    record = Document(
        id=_to_uuid(document_id) or uuid_lib.uuid4(),
        kb_id=_to_uuid(kb_id),
        owner_id=_to_uuid(owner_id),
        title=title,
        filename=filename,
        file_path=file_path,
        file_size=file_size,
        mime_type=mime_type,
        file_hash=file_hash,
        status=status,
    )
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    await db.refresh(record)
    await refresh_kb_stats(record.kb_id, db)
    return record


async def update_status(
    document: Document,
    status: str,
    db: AsyncSession,
    error_msg: str | None = None,
) -> Document:
    """推进状态: pending -> parsing -> chunking -> indexed / failed"""
    document.status = status
    document.error_msg = error_msg
    await db.commit()
    await db.refresh(document)
    return document


async def set_chunk_stats(document: Document, chunk_count: int, token_count: int, db: AsyncSession) -> Document:
    document.chunk_count = chunk_count
    document.token_count = token_count
    await db.commit()
    await db.refresh(document)
    return document


async def delete_document(document: Document, db: AsyncSession) -> bool:
    """删除文档（chunks 由外键级联清掉；磁盘文件由路由层负责删）"""
    await db.delete(document)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    await refresh_kb_stats(document.kb_id, db)
    return True


async def count_by_status(owner_id, db: AsyncSession, kb_id=None) -> dict[str, int]:
    """各状态的文档数量，给 GET /documents/status 用"""
    uid = _to_uuid(owner_id)
    if uid is None:
        return {}
    conditions = [Document.owner_id == uid, Document.deleted_at.is_(None)]
    if kb_id is not None:
        pk = _to_uuid(kb_id)
        if pk is None:
            return {}
        conditions.append(Document.kb_id == pk)

    result = await db.execute(
        select(Document.status, func.count()).where(*conditions).group_by(Document.status)
    )
    counts = {status: 0 for status in DocStatus}
    for status_value, count in result.all():
        counts[status_value] = int(count)
    return counts


async def list_document_paths(kb_id, owner_id, db: AsyncSession) -> list[str]:
    """某个知识库下全部文档的存储路径（给 /status 做「磁盘 ↔ 数据库」核对）

    不分页：核对必须看到全量，翻页会导致误判成文件缺失。
    """
    pk, uid = _to_uuid(kb_id), _to_uuid(owner_id)
    if pk is None or uid is None:
        return []
    result = await db.execute(
        select(Document.file_path)
        .where(Document.kb_id == pk, Document.owner_id == uid, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
    )
    return [row for row in result.scalars().all() if row]


async def list_chunks(document_id, owner_id, db: AsyncSession, limit: int = 3) -> list[Chunk]:
    """按 seq 升序取文档正文的前几块（详情预览用），同样带 owner_id"""
    did, uid = _to_uuid(document_id), _to_uuid(owner_id)
    if did is None or uid is None:
        return []
    result = await db.execute(
        select(Chunk)
        .where(Chunk.document_id == did, Chunk.owner_id == uid)
        .order_by(Chunk.seq)
        .limit(limit)
    )
    return list(result.scalars().all())


async def refresh_kb_stats(kb_id, db: AsyncSession, *, bump_version: bool = True) -> KnowledgeBase | None:
    """重算知识库统计并把版本号 +1（文档增删/重索引后调用）

    「版本信息」就是这里来的：前端拿 version 判断缓存要不要刷新。
    """
    pk = _to_uuid(kb_id)
    if pk is None:
        return None

    result = await db.execute(
        select(
            func.count(Document.id),
            func.coalesce(func.sum(Document.chunk_count), 0),
        ).where(Document.kb_id == pk, Document.deleted_at.is_(None))
    )
    document_count, chunk_count = result.one()

    kb = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == pk).with_for_update())
    ).scalar_one_or_none()
    if kb is None:
        return None

    kb.document_count = int(document_count or 0)
    kb.chunk_count = int(chunk_count or 0)
    if bump_version:
        kb.version += 1
        kb.last_indexed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(kb)
    return kb

async def get_document_by_userid_and_kdid(
    user_id: str,
    kb_id: str,
    db: AsyncSession
)-> list[str]:
    pass
