"""流式输出（SSE）协议层

一帧长这样（**事件名 + 统一载荷**，两个都要）：

    event: token
    id: 7
    data: {"status":200,"msg":"chunk","data":{"text":"你好"}}
    <空行>

为什么不能只用事件名：整个项目的前端解包逻辑是按 `{status,msg,data}` 写的（DEVELOPMENT §3.6），
流式接口要是另搞一套形状，前端就得为它开后门；为什么不能只用统一载荷：
token / meta / references / error / done 五类事件混在一个流里，前端必须能分流处理，
靠 `msg` 字符串判断类型是迟早会写错的活。

三个必须写清楚的坑（都是原来那版会踩的）：
  1. **`stream_mode` 不是 chat model 的参数**。它是 LangGraph「图」的概念：
     `agent.astream(..., stream_mode="messages")` 每帧给 `(message_chunk, metadata)`；
     而 `ChatModel.astream(...)` 的多余 kwargs 会一路透传进厂商 SDK，轻则被忽略、
     重则 TypeError。所以这里分成两个函数，各用各的正确签名，不做"通用封装"。
  2. **dict 消息不能 `HumanMessage(**m)`**。那样 `{"role":"assistant",...}` 会被悄悄变成
     HumanMessage —— 模型把自己上一轮的回答当成用户说的话，多轮对话直接错位，而且不报错。
     一律走 `convert_to_messages`。
  3. **异常原文不能进流**。`str(e)` 里可能有 api_key、内网地址、厂商报文。
     对外只给 `trace_id` + 通用文案，详情进日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid as uuid_lib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from fastapi.responses import StreamingResponse
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import convert_to_messages

logger = logging.getLogger("app.rag.output")

# 事件名常量：前端按这些名字挂监听，改名等于破坏协议
EVENT_META = "meta"
EVENT_TOKEN = "token"
EVENT_REFERENCES = "references"
EVENT_DONE = "done"
EVENT_ERROR = "error"

# 帧序号：每帧 +1 写进 `id:`。浏览器/前端重连时会带 `Last-Event-ID` 回来，
# 服务端据此决定"从哪条消息的哪个位置续播"——真正的续传要等 M5 会话落库后做（见 TODO.md M5）。


def envelope(status: int, msg: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """和 ApiResponse 同形的载荷。流式里不套外层对象，但**字段含义必须一致**"""
    return {"status": status, "msg": msg, "data": data or {}}


def format_sse(event: str, payload: Mapping[str, Any], *, event_id: str | None = None) -> str:
    """拼一帧。`data` 必须是单行 JSON —— 换行由 json.dumps 转义，所以不会把帧切错

    注意结尾是两个换行：SSE 靠空行分帧，少一个换行前端就会把两帧粘成一帧。
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


def to_chat_messages(messages: Sequence[Any]) -> list[BaseMessage]:
    """BaseMessage / {"role","content"} dict / (role, content) 元组，统统转成真正的消息对象"""
    normalized: list[Any] = []
    for item in messages:
        if isinstance(item, BaseMessage):
            normalized.append(item)
        elif isinstance(item, Mapping):
            raw = dict(item)
            # convert_to_messages 要求 dict 必须同时有 role 和 content，缺 role 直接抛 ValueError。
            # 上游（前端传的草稿消息、脚本里的简单 {"content": "..."}）经常只给内容，
            # 所以这里兜成 user：它是唯一"缺角色也说得通"的默认，而不是让整条流挂掉。
            raw.setdefault("role", "user")
            normalized.append(raw)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            normalized.append({"role": str(item[0]), "content": str(item[1])})
        else:  # 纯字符串等畸形入参交给 convert_to_messages 报错，不在这里猜意图
            normalized.append(item)
    return convert_to_messages(normalized)


def sse_headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """反代和浏览器都必须按流式来配，否则"流式"会变成"一次性蹦出来" """
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # nginx：不缓冲，否则整段答案攒完才发（手机端表现为长时间空白）
    }
    headers.update(dict(extra or {}))
    return headers


def sse_response(
    generator: AsyncIterator[str],
    *,
    headers: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> StreamingResponse:
    """把事件流包成响应。

    status_code 必须 200：SSE 里出错走 error 事件，不用非 200 状态码 ——
    浏览器 `EventSource` 拿到非 200 会直接断开并进入重连风暴，而不是告诉你哪一步错了。
    """
    merged = sse_headers(headers)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        status_code=status_code,
        headers=merged,
    )


class _Framer:
    """帧序号与"只发一次"的收尾控制，避免 done/error 之后又冒出一帧"""

    def __init__(self) -> None:
        self.seq = 0
        self.closed = False

    def frame(self, event: str, payload: Mapping[str, Any]) -> str:
        self.seq += 1
        if event in (EVENT_DONE, EVENT_ERROR):
            self.closed = True
        return format_sse(event, payload, event_id=str(self.seq))


async def stream_chat_tokens(
    *,
    model,
    messages: Sequence[Any],
    meta: Mapping[str, Any] | None = None,
    references: Sequence[Mapping[str, Any]] | None = None,
    heartbeat_seconds: float = 0,
    on_text: Callable[[str], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AsyncIterator[str]:
    """逐字流式：**chat model 路径**（不接 stream_mode，也不接 agent）

    典型用法（M5 的对话路由）：先检索 → 参考资料塞进 messages → 这个函数边生成边发，
    最后一帧把引用列表发给前端点回原文。

    两个钩子是为 M5 的 /stop 与落库加的（协议本身没变）：
      - on_text: 每生成一段文本回调一次。调用方要拿全文落库，不能等流结束后再把发出去的帧拼回来；
      - should_stop: 每次取下一个 chunk 前问一次。返回 True 就正常收尾发 done 帧，而不是抛取消异常 ——
        半截答案要能落库，前端也要收到一个明确的结束帧。
    """
    agen: Any = None  # 提前 break（被 /stop）时要在 finally 里显式关掉，见函数末尾
    framer = _Framer()
    payload = envelope(200, "start", dict(meta or {}))
    yield framer.frame(EVENT_META, payload)

    chat_messages = to_chat_messages(messages)
    collected: list[str] = []
    pending: asyncio.Future | None = None
    agen: Any = None
    try:
        agen = model.astream(chat_messages).__aiter__()  # 注意：这里不传 stream_mode
        while True:
            if pending is None:
                pending = asyncio.ensure_future(agen.__anext__())
            try:
                if heartbeat_seconds > 0:
                    # shield 很关键：直接 wait_for(agen.__anext__()) 超时会**取消**这次读取，
                    # 模型流就被心跳打断了。shield 后超时只发 ping，读取继续跑。
                    chunk = await asyncio.wait_for(asyncio.shield(pending), heartbeat_seconds)
                else:
                    chunk = await pending
            except TimeoutError:
                yield ": ping\n\n"  # 注释帧：不触发前端事件，但让代理知道连接还活着
                continue
            except StopAsyncIteration:
                break  # 正常读完；StopAsyncIteration 绝不能往生成器外抛，否则变 RuntimeError
            if should_stop is not None and should_stop():
                break  # 正常收尾：下面照常发 references / done
            pending = None
            text = _chunk_text(chunk)
            if text:
                collected.append(text)
                if on_text is not None:
                    on_text(text)
                yield framer.frame(EVENT_TOKEN, envelope(200, "chunk", {"text": text}))

        if references:
            yield framer.frame(EVENT_REFERENCES, envelope(200, "references", {"items": list(references)}))
        yield framer.frame(EVENT_DONE, envelope(200, "done", {"text": "".join(collected)}))
    except StopAsyncIteration:
        yield framer.frame(EVENT_DONE, envelope(200, "done", {"text": "".join(collected)}))
    except Exception:  # noqa: BLE001 —— 流里出错的唯一正确姿势：发 error 帧并收尾
        trace_id = uuid_lib.uuid4().hex[:8]
        logger.exception("流式生成失败 trace_id=%s", trace_id)
        if not framer.closed:
            yield framer.frame(EVENT_ERROR, envelope(500, "生成中断，请重试", {
                "trace_id": trace_id,
                "retryable": True,
                "partial_text": "".join(collected),
            }))
    finally:
        # 客户端中途断开：把还在跑的读取 task 收掉，别留孤儿
        if pending is not None and not pending.done():
            pending.cancel()
        if agen is not None:
            # 被 should_stop 提前 break 时模型流还开着；交给 GC 关闭会变成
            # "async generator ignored GeneratorExit" 这类噪声，手机上尤其明显（切后台=常态）
            await agen.aclose()


def _chunk_text(chunk: Any) -> str:
    """从 AIMessageChunk 里取增量文本；空 chunk 返回空串"""
    content = getattr(chunk, "content", None)
    if content is None and isinstance(chunk, Mapping):
        content = chunk.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # thinking/blocks 形态的 content：只取文本块
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


async def stream_agent_events(
    *,
    agent,
    messages: Sequence[Any] | Mapping[str, Any],
    stream_mode: str = "messages",
    config: Mapping[str, Any] | None = None,
    references: Sequence[Mapping[str, Any]] | None = None,
    heartbeat_seconds: float = 0,
) -> AsyncIterator[str]:
    """逐字流式：**LangGraph agent 路径**（`stream_mode` 只有这里才该出现）

    `stream_mode="messages"` 每帧是 `(message, metadata)` 二元组，metadata 里能看出这条来自
    哪个节点；工具消息和 AI 文本混在一起，所以只把 AI 的文本增量发给用户，
    工具调用细节要展示就得另开一个事件名（M8 再定，别现在瞎猜前端怎么用）。
    """
    framer = _Framer()
    yield framer.frame(EVENT_META, envelope(200, "start", {"stream_mode": stream_mode}))
    collected: list[str] = []
    try:
        async for piece in agent.astream(messages, config=config, stream_mode=stream_mode):
            chunk = piece[0] if isinstance(piece, tuple) and piece else piece
            if _is_tool_chunk(chunk):
                continue
            text = _chunk_text(chunk)
            if text:
                collected.append(text)
                yield framer.frame(EVENT_TOKEN, envelope(200, "chunk", {"text": text}))
        if references:
            yield framer.frame(EVENT_REFERENCES, envelope(200, "references", {"items": list(references)}))
        yield framer.frame(EVENT_DONE, envelope(200, "done", {"text": "".join(collected)}))
    except Exception:  # noqa: BLE001
        trace_id = uuid_lib.uuid4().hex[:8]
        logger.exception("agent 流式失败 trace_id=%s", trace_id)
        if not framer.closed:
            yield framer.frame(EVENT_ERROR, envelope(500, "生成中断，请重试", {"trace_id": trace_id}))


def _is_tool_chunk(chunk: Any) -> bool:
    """工具返回的消息不该当成"模型在说话"流出去"""
    name = type(chunk).__name__
    if "ToolMessage" in name:
        return True
    return bool(getattr(chunk, "tool_call_chunks", None))


__all__ = [
    "EVENT_DONE",
    "EVENT_ERROR",
    "EVENT_META",
    "EVENT_REFERENCES",
    "EVENT_TOKEN",
    "envelope",
    "format_sse",
    "sse_headers",
    "sse_response",
    "stream_agent_events",
    "stream_chat_tokens",
    "to_chat_messages",
]
