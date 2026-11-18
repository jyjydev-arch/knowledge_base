"""chunks 表的数据访问

只读写数据库、不抛 HTTP 异常（DEVELOPMENT §2）。切块的算法在 app/services/chunker.py，
这里只负责落库和取回。

**幂等是这里的重点**：documents.status 失败后会被重新索引，同一篇文档重复入库
必须得到同样的结果，所以入口只有 replace_*（先按 document_id 删干净再插），
没有"往后面追加"的口子 —— 留着追加口早晚有人写出翻倍的数据。
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def replace_document_chunks(document, pieces, db: AsyncSession) -> tuple[int, int]:
    """按 document_id 全量替换切块，返回 (块数, token 总数)

    document 传 Document 对象（需要它的 kb_id/owner_id 冗余列），pieces 是 ChunkPiece 列表。
    删和插放在同一个事务里：中途失败不会留下"旧块没了新块也没进来"的空档。
    """
    doc_uuid = _to_uuid(document.id)
    await db.execute(delete(Chunk).where(Chunk.document_id == doc_uuid))

    total_tokens = 0
    for piece in pieces:
        content = (piece.content or "").strip()
        if not content:
            continue
        total_tokens += piece.token_count
        db.add(
            Chunk(
                document_id=doc_uuid,
                kb_id=_to_uuid(document.kb_id),
                owner_id=_to_uuid(document.owner_id),
                seq=piece.seq,
                content=content,
                token_count=piece.token_count,
                page=piece.page,
                heading=piece.heading,
                extra=dict(piece.extra or {}),
            )
        )
    await db.commit()
    return len(pieces), total_tokens


async def delete_document_chunks(document_id, db: AsyncSession) -> int:
    """只删块（向量库那边由 indexing 侧按同一 document_id 清理）"""
    pk = _to_uuid(document_id)
    if pk is None:
        return 0
    result = await db.execute(delete(Chunk).where(Chunk.document_id == pk))
    await db.commit()
    return int(result.rowcount or 0)


async def count_document_chunks(document_id, db: AsyncSession) -> int:
    pk = _to_uuid(document_id)
    if pk is None:
        return 0
    result = await db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == pk)
    )
    return int(result.scalar_one())


async def list_document_chunks(document_id, db: AsyncSession, *, limit: int = 5000, offset: int = 0) -> list[Chunk]:
    pk = _to_uuid(document_id)
    if pk is None:
        return []
    result = await db.execute(
        select(Chunk).where(Chunk.document_id == pk).order_by(Chunk.seq).offset(offset).limit(limit)
    )
    return list(result.scalars().all())


async def list_chunks_for_embedding(
    document_id, db: AsyncSession, *, only_without_vector: bool = False
) -> list[Chunk]:
    """给向量化任务取块（按 seq 顺序，保证同一文档多次跑的顺序一致）

    only_without_vector 先留着不实现：`chunks.embedding` 列还没定（维度一旦定了不能改），
    现在向量存在 PGVector 自己的表里，靠 metadata 的 chunk_id 对应回来。
    """
    if only_without_vector:
        raise NotImplementedError("chunks.embedding 列未启用；向量目前放在 PGVector 的表里")
    return await list_document_chunks(document_id, db)


async def search_chunks(kb_ids, terms, db: AsyncSession, *, candidate_limit: int = 80) -> list[dict]:
    """关键词检索候选块（M4 的第一版车道，形状与以后的向量版一致）

    三条不能省的地方：
      1. **kb_ids 必传且必须是 uuid 列表**：调用方（app/rag/retrieval.py）给的必须是
         「这个用户读得到的库」，越权过滤不能指望这里兜底，但空列表必须直接返回空 ——
         否则 `IN ()` 在部分驱动上会变成不带条件，等于全库检索；
      2. **JOIN documents 过滤 deleted_at**：文档是软删的，块要等真删才级联走，
         不 join 就会把已删文档的内容答给用户；
      3. **转义 LIKE 通配符**：用户问题里的 % 和 _ 不转义就变成"匹配任意"，
         既让检索结果失控又能拖垮查询（前缀 % 本来就走不了普通索引）。

    只选必要列、不返回 ORM 对象：异步路由里碰 chunk.document 会触发懒加载报
    MissingGreenlet，与其在 service 层小心避开，不如这里就不给关系对象。
    排序交给上层（按词频算分，SQL 里算不动），这里只保证"候选集有界"。
    """
    pks = [pk for pk in (_to_uuid(item) for item in (kb_ids or [])) if pk is not None]
    words = [str(term).strip() for term in (terms or []) if str(term).strip()]
    if not pks or not words:
        return []

    likes = []
    for word in words[:12]:  # 词数上限：中文分词会产出很多 2-gram，全 OR 出去 SQL 会大得没边
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        likes.append(Chunk.content.ilike(f"%{escaped}%", escape="\\"))

    result = await db.execute(
        select(
            Chunk.id,
            Chunk.kb_id,
            Chunk.document_id,
            Chunk.owner_id,
            Chunk.seq,
            Chunk.content,
            Chunk.page,
            Chunk.heading,
            Chunk.token_count,
            Document.title,
            Document.filename,
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.kb_id.in_(pks),
            Document.deleted_at.is_(None),
            or_(*likes),
        )
        .limit(max(1, int(candidate_limit)))
    )
    hits: list[dict] = []
    for row in result.all():
        hits.append(
            {
                "chunk_id": row[0],
                "kb_id": row[1],
                "document_id": row[2],
                "owner_id": row[3],
                "seq": int(row[4] or 0),
                "content": row[5] or "",
                "page": row[6],
                "heading": row[7],
                "token_count": int(row[8] or 0),
                "document_title": row[9] or "",
                "document_filename": row[10] or "",
            }
        )
    return hits


__all__ = [
    "count_document_chunks",
    "search_chunks",
    "delete_document_chunks",
    "list_chunks_for_embedding",
    "list_document_chunks",
    "replace_document_chunks",
]
