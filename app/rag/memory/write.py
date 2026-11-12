"""长期记忆写入的函数式入口（薄封装，逻辑都在 store.py）

保留你原来那两个名字的语义，但做了三处收紧：
  1. **user_id 必填**。原来默认 "default_user"，忘了传就会把所有人的记忆写进同一个池子，
     下个用户读出来的是别人的内容 —— 这是最难查也最严重的泄漏。门面和后端都不给默认值。
  2. LLM 抽取从"自己 new ChatOpenAI + os.environ"改成走 app/rag/llm.py：
     没配 key / 调用失败时**自动降级成规则式摘要**，接口不会 500。
  3. 写完统一走去重 + 容量淘汰，不要在调用方各自实现一遍。
"""

from __future__ import annotations

from typing import Any

from fastapi.concurrency import run_in_threadpool

from app.rag.memory.base import ChatRole
from app.rag.memory.store import LongTermMemory, get_long_term_memory


def write_memory(
    content: str,
    user_id: str,
    memory_type: str = "conversation",
    metadata: dict[str, Any] | None = None,
    deduplicate_threshold: float | None = None,
    *,
    store: LongTermMemory | None = None,
) -> str | None:
    """写一条长期记忆，返回 id；被去重丢掉/后端不可用返回 None

    deduplicate_threshold 显式传值 = 这一次用这个阈值判重（0 表示不判重），
    不传就用配置里的 MEMORY_DEDUP_THRESHOLD。
    """
    facade = store or get_long_term_memory()
    return facade.write(user_id, content, metadata, memory_type=memory_type, dedupe_threshold=deduplicate_threshold)


def write_turn_summary(
    user_input: str,
    assistant_reply: str,
    user_id: str,
    *,
    conversation_id: str | None = None,
    kb_id: str | None = None,
) -> list[str]:
    """最省事的记法：直接记这一轮（有 LLM 就抽事实，没有就规则式摘要）"""
    facade = get_long_term_memory()
    return facade.remember_turn(
        user_id, user_input, assistant_reply, conversation_id=conversation_id, kb_id=kb_id
    )


def extract_and_write_facts(
    user_input: str,
    assistant_reply: str,
    user_id: str,
    *,
    conversation_id: str | None = None,
    fallback_to_summary: bool = True,
) -> list[str]:
    """只抽「用户事实/偏好」再写入；模型不可用时按参数决定要不要退回记摘要

    返回真正写进去的 id 列表（可能为空：这一轮没有值得长期记住的东西，是正常的）。
    """
    facade = get_long_term_memory()
    return facade.remember_turn(
        user_id,
        user_input,
        assistant_reply,
        conversation_id=conversation_id,
        extract_facts=True if fallback_to_summary else None,
    )


async def awrite_memory(content: str, user_id: str, **kwargs: Any) -> str | None:
    """异步版本：PGVector 是同步实现，直接在协程里跑会卡住事件循环"""
    return await run_in_threadpool(write_memory, content, user_id, **kwargs)


async def aextract_and_write_facts(user_input: str, assistant_reply: str, user_id: str, **kwargs: Any) -> list[str]:
    return await run_in_threadpool(extract_and_write_facts, user_input, assistant_reply, user_id, **kwargs)


__all__ = [
    "ChatRole",
    "awrite_memory",
    "aextract_and_write_facts",
    "extract_and_write_facts",
    "write_memory",
    "write_turn_summary",
]
