"""长期记忆检索的函数式入口（薄封装，逻辑都在 store.py）

阈值口径要说清楚，这是最容易调错的一个参数：
  - pgvector 后端：返回的是**距离**，COSINE 下 `相似度 = 1 - distance`，范围约 0~1。
    经验值（text-embedding-3-small）：强相关 ~0.75+，一般 0.5~0.7，弱相关 <0.4。
  - local 后端：词面覆盖率，分布整体偏低，所以同一个配置值会按后端的 threshold_scale
    折算（0.5 在 local 上实际等于 0.3），两个后端口径统一，配置只写一个数。
"""

from __future__ import annotations

from fastapi.concurrency import run_in_threadpool

from app.rag.memory.base import MemoryItem
from app.rag.memory.store import PROMPT_HEADER, LongTermMemory, get_long_term_memory


def read_memory(
    query: str,
    user_id: str,
    top_k: int | None = None,
    score_threshold: float | None = None,
    *,
    memory_type: str | None = None,
    store: LongTermMemory | None = None,
) -> list[str]:
    """取和 query 相关的记忆文本；没有相关记忆返回空列表（不是 None，省得调用方判空）"""
    facade = store or get_long_term_memory()
    return facade.read(
        user_id, query, limit=top_k, threshold=score_threshold, memory_type=memory_type
    )


def search_memory(
    query: str,
    user_id: str,
    top_k: int | None = None,
    score_threshold: float | None = None,
    *,
    memory_type: str | None = None,
    store: LongTermMemory | None = None,
) -> list[MemoryItem]:
    """带 id 和相似度的版本：给"这条记忆是谁、什么时候、像不像"这类调试/管理场景用"""
    facade = store or get_long_term_memory()
    return facade.search(user_id, query, limit=top_k, threshold=score_threshold, memory_type=memory_type)


def format_memories_for_prompt(memories: list[str], header: str = PROMPT_HEADER) -> str:
    """把记忆列表拼成一段可直接塞进 system prompt 的文字；空列表返回空串"""
    if not memories:
        return ""
    lines = [header]
    lines.extend(f"{index}. {text}" for index, text in enumerate(memories, 1))
    return "\n".join(lines)


def memories_to_prompt(user_id: str, query: str, top_k: int | None = None) -> str:
    """一步到位：检索 + 拼装（agent 里就一行这个）"""
    facade = get_long_term_memory()
    return facade.format_for_prompt(user_id, query, limit=top_k)


async def aread_memory(query: str, user_id: str, **kwargs) -> list[str]:
    """异步版本。同步 PGVector 必须在协程外面跑，别在事件循环里直连数据库"""
    return await run_in_threadpool(read_memory, query, user_id, **kwargs)


async def asearch_memory(query: str, user_id: str, **kwargs) -> list[MemoryItem]:
    return await run_in_threadpool(search_memory, query, user_id, **kwargs)


__all__ = [
    "aread_memory",
    "asearch_memory",
    "format_memories_for_prompt",
    "memories_to_prompt",
    "read_memory",
    "search_memory",
]
