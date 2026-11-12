"""长期记忆的 pgvector 车道：MemoryBackend 的 PG 实现

和 LocalJsonBackend 是同一个接口，差别只在「真语义向量 + 走索引 + 多进程共享」。
这里刻意做成**懒连接**：构造对象不碰数据库，第一次真正读写时才拿单例 vectorstore。
这样 `MEMORY_BACKEND=pgvector` 写在 .env 里也不会让应用启动失败（以前 store.py 就是
在模块导入期建池子，库还没建就整条链路起不来）。

注意 PGVector 的 score 语义：`similarity_search_with_score` 返回的是**距离**，
COSINE 策略下 `distance = 1 - 余弦相似度`，所以 `相似度 = 1 - distance`，范围约 0~1（越大越像）。
换成 EUCLIDEAN/NEGATIVE_INNER_PRODUCT 这行换算就得跟着改，别照抄。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.core.config import settings
from app.rag.memory.base import (
    BackendCapabilityError,
    MemoryBackend,
    MemoryError,
    MemoryItem,
)

logger = logging.getLogger("app.rag.memory")

# 只有列举/计数/删除需要碰表；add/search 全部用 PGVector 的公开 API，升级不容易碎
_SELECT_SQL = """
select e.id, e.document, e.cmetadata
  from langchain_pg_embedding e
  join langchain_pg_collection c on c.uuid = e.collection_id
 where c.name = :collection
   and e.cmetadata ->> 'user_id' = :user_id
 order by e.cmetadata ->> 'created_at' desc
 limit :limit offset :offset
"""

_COUNT_SQL = """
select count(*)
  from langchain_pg_embedding e
  join langchain_pg_collection c on c.uuid = e.collection_id
 where c.name = :collection
   and e.cmetadata ->> 'user_id' = :user_id
"""

_DELETE_SQL = """
delete from langchain_pg_embedding e
 where e.id in :ids
   and exists (
       select 1 from langchain_pg_collection c
        where c.uuid = e.collection_id and c.name = :collection
   )
   and e.cmetadata ->> 'user_id' = :user_id
"""


class PgVectorBackend(MemoryBackend):
    """按 `(collection, cmetadata->>'user_id')` 硬隔离，用户之间永远检不到对方的记忆"""

    name = "pgvector"
    default_threshold = 0.50  # 语义向量的相似度分布和词面 hash 不同，阈值口径见 scale_threshold
    threshold_scale = 1.0

    def __init__(self, vectorstore=None) -> None:
        self._vs = vectorstore  # None = 还没连库；传实例则用于测试注入

    @property
    def vs(self):
        if self._vs is None:
            from app.rag.memory.save import get_vectorstore

            self._vs = get_vectorstore()
        return self._vs

    # ---------- 写 ----------
    def add(self, items: Sequence[MemoryItem], dedupe_threshold: float | None = None) -> list[str]:
        from langchain_core.documents import Document

        dedupe = settings.MEMORY_DEDUP_THRESHOLD if dedupe_threshold is None else dedupe_threshold
        stored: list[str] = []
        for item in items:
            text = item.text.strip()
            if not text:
                continue
            if self._is_duplicate(item.user_id, text, dedupe):
                continue
            metadata = {
                "user_id": item.user_id,
                "memory_type": item.memory_type,
                "created_at": item.created_at,
                **item.metadata,
            }
            self.vs.add_documents([Document(page_content=text, metadata=metadata, id=item.id)])
            stored.append(item.id)
        return stored

    def _is_duplicate(self, user_id: str, text: str, threshold: float | None) -> bool:
        """写之前先比一眼：默认 0.95，低于它就是「同一件事换个说法」，不重复灌"""
        threshold = settings.MEMORY_DEDUP_THRESHOLD if threshold is None else threshold
        if not threshold:
            return False
        hits = self.search(user_id, text, top_k=1, threshold=threshold)
        return bool(hits)

    # ---------- 读 ----------
    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
        threshold: float | None = None,
        memory_type: str | None = None,
    ) -> list[MemoryItem]:
        if not query.strip() or top_k <= 0:
            return []
        cutoff = self.scale_threshold(threshold)
        flt: dict = {"user_id": user_id}
        if memory_type:
            flt["memory_type"] = memory_type
        # 阈值是"过滤"动作，多取一点再筛，免得 top_k 全被卡掉后一条都不剩
        pairs = self.vs.similarity_search_with_score(query, k=max(top_k * 3, top_k + 2), filter=flt)
        results: list[MemoryItem] = []
        for doc, distance in pairs:
            similarity = 1.0 - float(distance)
            if similarity < cutoff:
                continue
            meta = dict(doc.metadata or {})
            results.append(
                MemoryItem(
                    id=str(doc.id or meta.get("id") or ""),
                    text=doc.page_content,
                    user_id=str(meta.get("user_id") or user_id),
                    memory_type=str(meta.get("memory_type") or "conversation"),
                    created_at=str(meta.get("created_at") or ""),
                    metadata={k: v for k, v in meta.items() if k not in {"user_id", "memory_type", "created_at"}},
                    score=similarity,
                )
            )
        results.sort(key=lambda item: item.score or 0.0, reverse=True)
        return results[:top_k]

    # ---------- 管理口（走 SQL，PGVector 没提供带过滤的列举） ----------
    def _rows(self, sql: str, params: dict):
        from sqlalchemy import bindparam, text

        try:
            statement = text(sql)
            if ":ids" in sql:
                statement = statement.bindparams(bindparam("ids", expanding=True))
            session = self.vs.session_maker
            with session() as db_session:
                return db_session.execute(statement, params).all()
        except BackendCapabilityError:
            raise
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 表结构/版本差异统一收口，别让接口 500
            logger.warning("pgvector 后端 SQL 操作失败: %s: %s", type(exc).__name__, str(exc)[:200])
            raise BackendCapabilityError(f"pgvector 后端不支持该操作: {exc}") from exc

    def list_items(self, user_id: str, limit: int = 50, offset: int = 0) -> list[MemoryItem]:
        rows = self._rows(
            _SELECT_SQL,
            {"collection": settings.MEMORY_PG_COLLECTION, "user_id": user_id, "limit": limit, "offset": offset},
        )
        items: list[MemoryItem] = []
        for row in rows:
            meta = dict(row[2] or {})
            items.append(
                MemoryItem(
                    id=str(row[0]),
                    text=str(row[1] or ""),
                    user_id=str(meta.get("user_id") or user_id),
                    memory_type=str(meta.get("memory_type") or "conversation"),
                    created_at=str(meta.get("created_at") or ""),
                    metadata={k: v for k, v in meta.items() if k not in {"user_id", "memory_type", "created_at"}},
                )
            )
        return items

    def count(self, user_id: str) -> int:
        rows = self._rows(
            _COUNT_SQL, {"collection": settings.MEMORY_PG_COLLECTION, "user_id": user_id}
        )
        return int(rows[0][0]) if rows else 0

    def delete(self, user_id: str, memory_ids: Sequence[str]) -> int:
        ids = [str(memory_id) for memory_id in memory_ids if str(memory_id).strip()]
        if not ids:
            return 0
        from sqlalchemy import bindparam, text

        statement = text(_DELETE_SQL).bindparams(bindparam("ids", expanding=True))
        params = {"collection": settings.MEMORY_PG_COLLECTION, "user_id": user_id, "ids": ids}
        try:
            session = self.vs.session_maker
            with session() as db_session:
                result = db_session.execute(statement, params)
                db_session.commit()
                return int(result.rowcount or 0)  # SQL 里已限定 user_id，删不到别人的记忆
        except Exception as exc:  # noqa: BLE001
            logger.warning("pgvector 后端删除失败: %s: %s", type(exc).__name__, str(exc)[:200])
            raise BackendCapabilityError(f"pgvector 后端不支持该操作: {exc}") from exc
