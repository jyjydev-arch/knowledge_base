"""一轮回答的编排：范围 → 检索 → 上下文 → 模型（流式/一次性）→ 落库

和 app/rag/chat.py 的分工：那边只管**存取**（会话/消息/窗口/摘要水位线），
这边管**一次问答从头到尾怎么走完**。路由只调 build_turn + stream_turn/answer_turn，
不在路由里拼 prompt、不碰 langchain（DEVELOPMENT §2）。

为什么流式车道必须自己开数据库 Session（session_factory）：
  SSE 的生成器活的时间比请求函数长得多——模型出字期间，请求级 Session 可能已经随依赖栈退出
  被还回连接池（FastAPI 对 `yield` 依赖的退出时机在不同小版本里改过行为，不能赌）。
  更实际的问题是：让一条数据库连接陪跑 30 秒的模型流，等于用池子换模型的等待时间，
  并发一上来池子就空转耗尽。所以**取用材料用请求级 Session，收尾落库用新开的一个**。

get_answer_model() 是唯一的模型取用点：测试替身换它（给个带 astream/ainvoke 的对象就行），
这样 SSE 分帧、消息转换、落库这条真链路能被完整测到，而不是把被测函数本身 mock 掉。
"""

from __future__ import annotations

import logging
import uuid as uuid_lib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.crud import chat_crud
from app.db.models import ChatSession
from app.rag import chat as chat_service
from app.rag import llm, retrieval
from app.rag.generation import ActiveGeneration, get_registry
from app.rag.memory.context import ChatContext
from app.rag.output.sse import (
    EVENT_ERROR,
    envelope,
    format_sse,
    stream_chat_tokens,
    to_chat_messages,
)
from app.services import kb_access

logger = logging.getLogger("app.rag.answer")

DEFAULT_CHAT_TITLE = "新对话"  # 与 models.ChatSession.title 的默认值一致


class AnswerError(Exception):
    """模型侧失败。对外只给 trace_id，异常原文进日志（sse.py 的同一条规矩）"""

    def __init__(self, msg: str = "生成失败，请稍后重试", *, retryable: bool = True) -> None:
        self.msg = msg
        self.retryable = retryable
        self.trace_id = uuid_lib.uuid4().hex[:8]
        super().__init__(msg)


@dataclass(slots=True)
class TurnPlan:
    """一轮问答的"计划"：所有需要在返回响应之前完成的事都在这里做完

    为什么要有这个中间产物：SSE 一旦开始发，HTTP 状态码就锁死在 200 了，
    会话不存在/没配模型这类错误必须在 build_turn 阶段以 404/503 的形式出去。
    """

    session: ChatSession
    context: ChatContext
    model: Any
    question: str
    user_id: Any
    sources: list[dict] = field(default_factory=list)
    retrieval: dict[str, Any] = field(default_factory=dict)
    needs_title: bool = False

    @property
    def session_id(self) -> Any:
        return self.session.id

    def messages(self) -> list[dict[str, str]]:
        return self.context.to_messages()

    def meta(self, *, generation_id: str | None = None) -> dict[str, Any]:
        """SSE 的 meta 帧载荷 / 非流式返回体里的调试信息"""
        data: dict[str, Any] = {
            "session_id": str(self.session_id),
            "kb_id": str(self.session.kb_id) if self.session.kb_id else None,
            "model": settings.LLM_MODEL,
            "retrieval": dict(self.retrieval),
            "context_tokens": self.context.token_estimate(),
        }
        if generation_id:
            data["generation_id"] = generation_id
        return data


def get_answer_model() -> Any:
    """取模型客户端（进程级缓存的，别每轮 new）。没配 key 会抛 MemoryNotConfiguredError"""
    return llm.get_chat_model()


async def resolve_kb_scope(session: ChatSession, user_id: Any, db: AsyncSession) -> list[Any]:
    """这一轮允许在哪些库里检索

    会话限定了库 → 只有那个库；没限定 → 该用户读得到的全部库（自己的 + 被共享的 + public）。
    权限判定只走 kb_access，这里不重复写规则。
    """
    if session.kb_id is not None:
        access = await kb_access.access_of(session.kb_id, user_id, db)
        if access is None or not access.can_read:
            return []  # 库被删了/授权被收了：宁可不带资料，也不能拿别人的内容回答
        return [session.kb_id]
    return await kb_access.accessible_kb_ids(user_id, db)


async def build_turn(
    session_id: Any,
    user_id: Any,
    question: str,
    db: AsyncSession,
    *,
    top_k: int | None = None,
) -> TurnPlan | None:
    """把这一轮要发给模型的东西准备好。会话不存在/不属于这个人 → None（路由据此 404）"""
    session = await chat_crud.get_session(session_id, user_id, db)
    if session is None:
        return None

    found = retrieval.RetrievedContext()
    if settings.CHAT_RETRIEVAL_ENABLED:
        kb_ids = await resolve_kb_scope(session, user_id, db)
        found = await retrieval.retrieve(question, kb_ids=kb_ids, db=db, top_k=top_k)

    context = await chat_service.load_context(
        session.id, user_id, question, db, documents_text=found.text, top_k=top_k
    )
    if context is None:  # 极端情况：会话刚被别的请求删掉
        return None

    return TurnPlan(
        session=session,
        context=context,
        model=get_answer_model(),
        question=question,
        user_id=user_id,
        sources=list(found.sources),
        retrieval=found.as_dict(),
        needs_title=(str(session.title or "").strip() == DEFAULT_CHAT_TITLE),
    )


def _text_of(reply: Any) -> str:
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 分块 content（thinking/多模态）：只取文本块
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


async def generate_answer_text(model: Any, messages: list[dict[str, str]]) -> str:
    """一次性车道。失败统一包成 AnswerError，不让厂商异常原文漏到响应里"""
    try:
        reply = await model.ainvoke(to_chat_messages(messages))
    except Exception as exc:  # noqa: BLE001
        logger.exception("模型一次性生成失败")
        raise AnswerError("模型暂时不可用，请稍后重试") from exc
    return _text_of(reply).strip()


async def finish_turn(
    plan: TurnPlan,
    answer: str,
    *,
    session_factory: Callable[[], Any],
    write_memory: bool | None = None,
    auto_title: bool = True,
) -> dict[str, Any]:
    """收尾落库：两条消息 + 摘要水位线 + 长期记忆 +（首轮）自动命名

    用**新开的 Session**（见模块 docstring）。落库失败不影响用户已经看到的回答，
    只记日志并把 stopped/persisted 标记如实带出去——比让前端看到流突然断掉更好排查。
    """
    async with session_factory() as session:
        record = await chat_service.record_turn(
            plan.session_id,
            plan.user_id,
            plan.question,
            answer,
            session,
            sources=plan.sources,
            write_long_term=write_memory,
        )
        titled = None
        if auto_title and plan.needs_title and record and record.get("appended"):
            titled = await chat_service.rename_chat(
                plan.session_id, plan.user_id, _title_from_question(plan.question), session
            )
    result = dict(record or {"appended": 0})
    if titled is not None:
        result["title"] = titled.title
    return result


def _title_from_question(question: str) -> str:
    """用问题当前缀命名。取第一行、按配置截断——长问题截完加省略号，别把半句话当标题"""
    first_line = (question or "").strip().splitlines()[0] if question.strip() else DEFAULT_CHAT_TITLE
    limit = max(4, int(settings.CHAT_AUTO_TITLE_CHARS))
    if len(first_line) <= limit:
        return first_line
    return first_line[:limit].rstrip() + "…"


async def stream_turn(
    plan: TurnPlan,
    *,
    session_factory: Callable[[], Any],
    generation: ActiveGeneration | None = None,
    write_memory: bool | None = None,
    auto_title: bool = True,
    heartbeat_seconds: float | None = None,
) -> AsyncIterator[str]:
    """流式车道：吐 SSE 帧，最后把完整回答落库

    /stop 的实现方式：不取消 asyncio.Task，而是查取消标志后**正常收尾**。
    原因是"任务被硬取消"会把中间状态停在半途（消息没落库、注册表没注销、流没 done 帧），
    而标志位方案能把已生成的部分照常存下来 —— 这正是 stop 接口的需求（保留半截答案）。
    """
    collected: list[str] = []
    stopped = False
    beat = settings.CHAT_STREAM_HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    try:
        async for frame in stream_chat_tokens(
            model=plan.model,
            messages=plan.messages(),
            meta=plan.meta(generation_id=generation.generation_id if generation else None),
            references=plan.sources,
            heartbeat_seconds=beat,
            on_text=lambda piece: _collect(collected, piece, generation),
            should_stop=(lambda: generation.is_cancelled) if generation is not None else None,
        ):
            yield frame
        stopped = bool(generation is not None and generation.is_cancelled)
        answer = "".join(collected).strip()
        if answer:
            record = await finish_turn(
                plan,
                answer,
                session_factory=session_factory,
                write_memory=write_memory,
                auto_title=auto_title,
            )
            logger.info(
                "会话 %s 落库完成 appended=%s stopped=%s", plan.session_id, record.get("appended"), stopped
            )
    except AnswerError as exc:
        yield _error_frame(exc.msg, trace_id=exc.trace_id, retryable=exc.retryable)
    except Exception:  # noqa: BLE001 —— 流里出错的唯一正确姿势：error 帧 + trace_id
        trace_id = uuid_lib.uuid4().hex[:8]
        logger.exception("流式问答失败 trace_id=%s session=%s", trace_id, plan.session_id)
        yield _error_frame("生成中断，请重试", trace_id=trace_id, retryable=True, partial="".join(collected))
    finally:
        if generation is not None:
            get_registry().release(generation)


def _collect(collected: list[str], piece: str, generation: ActiveGeneration | None) -> None:
    collected.append(piece)
    if generation is not None:
        generation.add_chars(len(piece))


def _error_frame(
    msg: str, *, trace_id: str, retryable: bool = True, partial: str = "", data: dict[str, Any] | None = None
) -> str:
    payload = {"trace_id": trace_id, "retryable": retryable}
    if partial:
        payload["partial_text"] = partial
    payload.update(data or {})
    return format_sse(EVENT_ERROR, envelope(500, msg, payload))


async def answer_turn(
    plan: TurnPlan,
    *,
    session_factory: Callable[[], Any],
    write_memory: bool | None = None,
    auto_title: bool = True,
) -> dict[str, Any]:
    """非流式车道：脚本、单测、以及不支持 SSE 的客户端用"""
    answer = await generate_answer_text(plan.model, plan.messages())
    record = await finish_turn(
        plan,
        answer,
        session_factory=session_factory,
        write_memory=write_memory,
        auto_title=auto_title,
    )
    return {
        "session_id": plan.session_id,
        "answer": answer,
        "sources": plan.sources,
        "retrieval_mode": str(plan.retrieval.get("mode") or "none"),
        "stopped": False,
        "turn": record,
    }


__all__ = [
    "AnswerError",
    "TurnPlan",
    "answer_turn",
    "build_turn",
    "finish_turn",
    "generate_answer_text",
    "get_answer_model",
    "resolve_kb_scope",
    "stream_turn",
]
