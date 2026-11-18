"""会话服务层（**DB 是唯一存储**）

你原来这个文件里是两个 `pass` + 一句「LangGraph 的 store 里每个对话独立，互不干扰」，
那句话里有两个误会，这里一并纠正：

1. **LangGraph 的 store ≠ 会话记忆**。store 是"跨线程的命名空间键值"（agent 工具自己读写），
   会话历史归 **checkpointer**，键是 `config={"configurable": {"thread_id": ...}}`。
   不接 checkpointer 的话，thread_id 传了也不存任何东西；接 InMemorySaver 的话
   重启即丢、多 worker 各存一份、**而且不带用户隔离**。
   真要用 LangGraph 的持久化，得装 `langgraph-checkpoint-postgres` 并把 thread_id 拼成
   f"{user_id}:{conversation_id}" —— 但**业务真相仍然在 chat_sessions/chat_messages 两张表**，
   因为前端要列会话、要翻页、要"关掉再打开还在"，那些都得能被 SQL 查询。
2. **`new_chat()` 不该返回"对话实例"**。agent / 记忆后端 / 向量库都必须是进程级单例
   （每个都带连接池），每对话 new 一个很快就变成几百个客户端。
   所以这里 new_chat() 的职责是：往 chat_sessions 插一行、返回 conversation_id。

短期窗口的**算法**在 `app/rag/memory/session.py:compress_window()`（纯函数），
本文件只负责**存取**；两条车道共用同一个算法，不会出现"同样聊十轮，两个车道压出两个结果"。

这里不碰 HTTP：查不到一律返回 None，由路由决定是 404 还是别的（DEVELOPMENT §2）。
"""

from __future__ import annotations

from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import chat_crud
from app.db.models import ChatMessage, ChatRole, ChatSession
from app.rag.memory import session as session_mod
from app.rag.memory.base import MemoryItem
from app.rag.memory.context import ChatContext, build_context
from app.rag.memory.store import LongTermMemory, get_long_term_memory
from app.services.text_metrics import estimate_tokens

# 一次最多折叠多少条消息：水位线之后如果堆了几百条（长期没压缩过），
# 分批压而不是在内存里全捞出来
FOLD_BATCH_MESSAGES = 40


def _policy() -> session_mod.WindowPolicy:
    return session_mod.WindowPolicy.from_settings()


async def new_chat(
    user_id: Any, db: AsyncSession, *, title: str | None = None, kb_id: Any = None
) -> ChatSession | None:
    """新建会话，返回会话对象（.id 就是前端要的 conversation_id）"""
    return await chat_crud.create_session(user_id, db, title=title, kb_id=kb_id)


async def get_chat(session_id: Any, user_id: Any, db: AsyncSession) -> ChatSession | None:
    """取会话。**按 (id, user_id) 双条件查**，别人的 id 在这里就是 None"""
    return await chat_crud.get_session(session_id, user_id, db)


async def list_chats(
    user_id: Any, db: AsyncSession, *, offset: int = 0, limit: int = 20, keyword: str | None = None
) -> tuple[list[ChatSession], int]:
    return await chat_crud.list_sessions(user_id, db, offset=offset, limit=limit, keyword=keyword)


async def rename_chat(session_id: Any, user_id: Any, title: str, db: AsyncSession) -> ChatSession | None:
    session = await chat_crud.get_session(session_id, user_id, db)
    if session is None:
        return None
    return await chat_crud.rename_session(session, title, db)


async def delete_chat(session_id: Any, user_id: Any, db: AsyncSession) -> bool:
    """删会话。消息由外键级联清理；长期记忆**不删**（那是用户级的事实，不属于某个会话）"""
    session = await chat_crud.get_session(session_id, user_id, db)
    if session is None:
        return False
    return await chat_crud.delete_session(session, db)


async def read_history(
    session_id: Any, user_id: Any, db: AsyncSession, *, limit: int = 50, before_seq: int | None = None
) -> list[ChatMessage] | None:
    """翻页读历史。返回 None 表示会话不存在或不属于这个用户（路由据此 404）"""
    if await chat_crud.get_session(session_id, user_id, db) is None:
        return None
    return await chat_crud.list_messages(session_id, user_id, db, limit=limit, before_seq=before_seq)


async def load_context(
    session_id: Any,
    user_id: Any,
    question: str,
    db: AsyncSession,
    *,
    documents_text: str = "",
    top_k: int | None = None,
    store: LongTermMemory | None = None,
) -> ChatContext | None:
    """拼出这一轮真正发给模型的上下文（历史从库里读 + 长期记忆语义检索）

    窗口取"水位线之后的最近若干条"：水位线之前的内容已经在 summary 里了，
    再读一遍就是重复占 token；本轮的新问题**还没入库**，所以不能包含它。
    """
    session = await chat_crud.get_session(session_id, user_id, db)
    if session is None:
        return None

    policy = _policy()
    tail = await chat_crud.list_messages_after(session, db, limit=policy.max_messages * 3)
    window = tail[-policy.max_messages:] if tail else []

    long_term: list[MemoryItem] = []
    memory = store or get_long_term_memory()
    if memory.enabled:
        # 记忆后端是同步的（读 JSONL / 连 PG 取 embedding），放线程池里跑：
        # 直接在事件循环里调会把整个 worker 卡住，一个用户的问答拖慢所有人
        long_term = await run_in_threadpool(lambda: memory.search(str(user_id), question, limit=top_k))

    return build_context(
        str(user_id),
        str(session.id),
        question,
        documents_text=documents_text,
        long_term=long_term,
        summary=session.summary or "",
        history=[
            session_mod.ChatMessage(role=message.role, content=message.content) for message in window
        ],
    )


async def record_turn(
    session_id: Any,
    user_id: Any,
    question: str,
    answer: str,
    db: AsyncSession,
    *,
    sources: list[dict] | None = None,
    write_long_term: bool | None = None,
    create_if_missing: bool = False,
    store: LongTermMemory | None = None,
) -> dict[str, Any] | None:
    """一轮问答收尾：两条消息入库 + 维持摘要水位线 + 按配置落长期记忆

    顺序有讲究：**先落库再压缩**。反过来万一压缩中途崩了，摘要里有一段而消息没存，
    下轮读出来就是"模型记得没发生过的事"。
    """
    session = await chat_crud.get_session(session_id, user_id, db)
    if session is None:
        if not create_if_missing:
            return None
        session = await chat_crud.create_session(user_id, db, kb_id=None)
        if session is None:
            return None

    user_message = await chat_crud.append_message(session, ChatRole.USER, question, db)
    assistant_message = await chat_crud.append_message(session, ChatRole.ASSISTANT, answer, db, sources=sources)
    if user_message is None and assistant_message is None:
        return {"session_id": str(session.id), "appended": 0, "compressed_turns": 0, "memory_ids": []}

    compressed_turns, summary_seq = await _fold_window(session, db)
    memory_ids: list[str] = []
    memory = store or get_long_term_memory()
    should_write = memory.enabled and answer.strip() != ""
    if write_long_term is not None:
        should_write = should_write and bool(write_long_term)
    if should_write:
        # remember_turn 可能真去调模型抽事实（最长等 LLM_TIMEOUT_SECONDS），必须进线程池，不能占着事件循环
        memory_ids = await run_in_threadpool(
            lambda: memory.remember_turn(
                str(user_id), question, answer, conversation_id=str(session.id)
            )
        )

    refreshed = await chat_crud.get_session(session.id, user_id, db) or session
    # 压缩之后的真实窗口大小 —— 前端/排错都要看这个数，不能报"本轮回答多少 token"
    remaining = await chat_crud.list_messages_after(refreshed, db, limit=_policy().max_messages * 3)
    window_tokens = sum(message.token_count for message in remaining) + estimate_tokens(refreshed.summary)
    return {
        "session_id": str(refreshed.id),
        "appended": int(user_message is not None) + int(assistant_message is not None),
        "message_count": int(refreshed.message_count or 0),
        "last_seq": int(refreshed.message_count or 0),
        "summary_chars": len(refreshed.summary or ""),
        "summary_seq": summary_seq,
        "compressed_turns": compressed_turns,
        "window_messages": len(remaining),
        "window_tokens": window_tokens,
        "memory_ids": memory_ids,
    }


async def _compress_in_thread(tail, summary: str, policy):
    """compress_window 是同步函数，但 auto_summarizer 里可能有一次模型调用 → 丢线程池跑

    算法本身仍然只有 session.compress_window() 一份（DEVELOPMENT §2），这里只是换个线程执行它。
    """
    def _run():
        rows = [
            session_mod.ChatMessage(role=item.role, content=item.content) for item in tail
        ]
        return session_mod.compress_window(rows, summary, policy, session_mod.auto_summarizer)

    return await run_in_threadpool(_run)


async def _fold_window(session: ChatSession, db: AsyncSession) -> tuple[int, int]:
    """按水位线推进摘要，返回 (本次折叠轮数, 最新水位线)

    分批而不是一次全压：会话可以聊上千轮，一次捞全部既占内存又会把一次请求拖成几秒。
    """
    policy = _policy()
    total_compressed = 0
    watermark = int(session.summary_seq or 0)
    for _round in range(5):
        tail = await chat_crud.list_messages_after(session, db, limit=FOLD_BATCH_MESSAGES)
        if not tail:
            break
        kept, new_summary, compressed = await _compress_in_thread(tail, session.summary or "", policy)
        if not compressed:
            break
        folded = tail[: len(tail) - len(kept)]
        if not folded:
            break
        watermark = int(folded[-1].seq)
        session = await chat_crud.save_summary(session, new_summary, db, upto_seq=watermark)
        total_compressed += compressed
    return total_compressed, watermark


__all__ = [
    "delete_chat",
    "get_chat",
    "list_chats",
    "load_context",
    "new_chat",
    "read_history",
    "record_turn",
    "rename_chat",
]
