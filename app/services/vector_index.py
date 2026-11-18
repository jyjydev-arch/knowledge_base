"""把 chunks 变成向量并写进 pgvector

这里是**知识库文档的向量**，和用户长期记忆（`app/rag/memory/`）是两回事，
两者只是都落在 pgvector 上，并且**用不同的 collection 隔开**：
混在一个 collection 里，检索时"用户记忆"和"文档片段"会互相命中，
按文档清理向量时还会把记忆条目一起带走。

对 `app/rag/memory/insert_vector.py` 那份草稿的三处修正：
1. **不再 drop_tables()**。PGVector 的 `drop_tables()` 是
   `Base.metadata.drop_all()` —— drop 的是 `langchain_pg_collection` +
   `langchain_pg_embedding` **两张整表**，而记忆车道用的正是这两张表。
   也就是说原来那条 `--recreate` 会顺手把用户所有长期记忆删光。
   这里只按 `metadata.document_id` 精确删除，重建幂等且不碰别人的数据。
2. **metadata 必带 `user_id / kb_id / document_id / chunk_id`**。缺这几项时检索层没法按用户
   过滤 —— 越权是这类系统最常见的洞；chunk_id 则是"向量 ↔ chunks 行"的唯一对应关系，
   没有它两份数据迟早对不上账。id 直接用 `chunks.id`，不用随机 uuid4。
3. 连接串/embedding 一律走 `settings`（`save._to_psycopg_dsn` + `save.build_embeddings`），
   不再写死 `localhost:6024` 那份 docker 示例值，也不再直接读 `os.environ`。

维度这件事仍然没做决定：`chunks.embedding` 列要不要加、`vector(N)` 用多少维，
取决于 embedding 模型选型，**一旦建列就不能改**（TODO.md M3）。当前向量只存在 PGVector 自己的
表里，靠 metadata 的 chunk_id 对应回来，所以业务表结构没被这个未定项绑死。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.config import settings
from app.db.crud import chunk_crud, document_crud
from app.db.models import Chunk
from app.rag.memory.base import MemoryError

logger = logging.getLogger("app.services.vector")

VECTOR_ID_KEY = "chunk_id"


def chunk_metadata(chunk: Chunk) -> dict[str, Any]:
    """一条向量必须携带的过滤字段

    `source` 之类给看的字段留着没意义（业务表里都有），只保留检索过滤真正要用的。
    """
    return {
        "user_id": str(chunk.owner_id),
        "kb_id": str(chunk.kb_id),
        "document_id": str(chunk.document_id),
        "chunk_id": str(chunk.id),
        "seq": int(chunk.seq),
        "page": chunk.page,
        "heading": chunk.heading or "",
    }


def build_records(chunks: Sequence[Chunk]) -> tuple[list[str], list[str], list[dict]]:
    """切成 (ids, texts, metadatas) 三元组，顺序严格一致

    PGVector 的 add_texts 是按下标对齐三个列表插入的，顺序错一位就是
    "这条向量对应到别的块" —— 那种脏数据比报错难查十倍。
    """
    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []
    for chunk in chunks:
        content = (chunk.content or "").strip()
        if not content:
            continue
        ids.append(str(chunk.id))
        texts.append(content)
        metadatas.append(chunk_metadata(chunk))
    return ids, texts, metadatas


def batches(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    limit = max(1, int(size or settings.VECTOR_INDEX_BATCH_SIZE))
    for start in range(0, len(items), limit):
        yield list(items[start : start + limit])


def _delete_by_document_ids_sync(vectorstore, document_ids: Sequence[str]) -> int:
    """按 metadata.document_id 精确删除，不碰别的 collection 也不碰记忆"""
    from sqlalchemy import bindparam, text

    statement = text(
        """
delete from langchain_pg_embedding e
 where e.cmetadata ->> 'document_id' in :ids
   and exists (
       select 1 from langchain_pg_collection c
        where c.uuid = e.collection_id and c.name = :collection
   )
"""
    ).bindparams(bindparam("ids", expanding=True))
    session = vectorstore.session_maker
    with session() as db_session:
        params = {"ids": list(document_ids), "collection": settings.DOCUMENT_VECTOR_COLLECTION}
        result = db_session.execute(statement, params)
        db_session.commit()
        return int(result.rowcount or 0)


async def index_document(
    document_id: Any,
    *,
    session: Any = None,
    vectorstore: Any = None,
    batch_size: int | None = None,
) -> dict:
    """给一篇文档建向量：先删旧的再写新的（幂等），返回 {ok, indexed, removed, reason}

    没有 embedding key 或连不上 PG 时**返回结果而不是抛异常** —— 这个函数会被
    上传后的后台任务调用，抛穿出去就等于"文件传成功了但接口报 500"。
    """
    from app.db.psql_config import AsyncSessionLocal

    store = vectorstore
    if store is None:
        try:
            from app.rag.memory.save import get_document_vectorstore

            store = get_document_vectorstore()
        except MemoryError as exc:
            logger.warning("向量车道不可用，跳过索引: %s", str(exc)[:200])
            return {"ok": False, "indexed": 0, "removed": 0, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 —— 三方库的意外失败也不能带崩调用方
            logger.exception("初始化向量库失败")
            return {"ok": False, "indexed": 0, "removed": 0, "reason": f"{type(exc).__name__}"}

    async def _run(db) -> dict:
        document = await document_crud.get_document_any(document_id, db)
        if document is None:
            return {"ok": False, "indexed": 0, "removed": 0, "reason": "document_not_found"}
        chunks = await chunk_crud.list_document_chunks(document.id, db)
        if not chunks:
            return {"ok": False, "indexed": 0, "removed": 0, "reason": "没有可向量化的切块（先跑解析入库）"}

        removed = await asyncio.to_thread(_delete_by_document_ids_sync, store, [str(document.id)])
        ids, texts, metadatas = build_records(chunks)
        written = 0
        size = batch_size or settings.VECTOR_INDEX_BATCH_SIZE
        for id_batch, text_batch, meta_batch in zip(
            batches(ids, size), batches(texts, size), batches(metadatas, size), strict=True
        ):
            await asyncio.to_thread(store.add_texts, text_batch, meta_batch, id_batch)
            written += len(id_batch)
        logger.info("向量写入完成 document_id=%s 条数=%d 清理旧向量=%d", document_id, written, removed)
        return {"ok": True, "indexed": written, "removed": removed, "reason": None}

    if session is not None:
        return await _run(session)
    async with AsyncSessionLocal() as db:
        return await _run(db)


async def remove_document_vectors(document_ids: Sequence[Any], *, vectorstore: Any = None) -> int:
    """删文档时清它的向量。传多个也行（删库时用）"""
    ids = [str(document_id) for document_id in document_ids if str(document_id).strip()]
    if not ids:
        return 0
    store = vectorstore
    if store is None:
        try:
            from app.rag.memory.save import get_document_vectorstore

            store = get_document_vectorstore()
        except MemoryError as exc:
            logger.warning("向量车道不可用，跳过清理: %s", str(exc)[:200])
            return 0
    try:
        return await asyncio.to_thread(_delete_by_document_ids_sync, store, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理向量失败（记录已删，向量留给下次重建）: %s", str(exc)[:200])
        return 0


def embedding_ready() -> tuple[bool, str]:
    """给 CLI/健康检查用：现在这条路能不能真跑"""
    if settings.DATABASE_URL.startswith("sqlite"):
        return False, "当前 DATABASE_URL 是 SQLite，pgvector 车道需要 PostgreSQL"
    if not settings.embedding_ready:
        return False, "未配置 EMBEDDING_API_KEY（DeepSeek 没有 embedding 接口，要另配一家）"
    return True, "可以跑"


__all__ = [
    "VECTOR_ID_KEY",
    "batches",
    "build_records",
    "chunk_metadata",
    "embedding_ready",
    "index_document",
    "remove_document_vectors",
]
