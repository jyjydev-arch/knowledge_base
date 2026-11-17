"""流式输出与结构化输出的测试（全程离线，用 langchain 的 FakeChatModel）

这里钉死的三件事，都是原来那版实现会静默出错的地方：
  1. `{"role":"assistant"}` 被 `HumanMessage(**m)` 变成"用户说的话"；
  2. 把 LangGraph 的 `stream_mode` 传给 chat model（多余 kwargs 会一路透传进厂商 SDK）；
  3. 异常原文 `str(e)` 直接进流（里面可能有 api_key、内网地址、厂商报文）。
"""

import asyncio
import json

import pytest
from langchain.chat_models.base import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from pydantic import ValidationError

from app.rag.output import sse
from app.rag.output.struct_response import (
    UNKNOWN_ANSWER_TEXT,
    AgentAnswer,
    SourceRef,
    StreamErrorData,
    bind_structured,
    response_format,
)

# --------------------------------------------------------------------------
# 帧格式
# --------------------------------------------------------------------------


def parse_frame(frame: str) -> dict:
    """按 SSE 规则解一帧，顺便验证帧本身是合法的"""
    out = {"event": None, "id": None, "data": None}
    body = frame.rstrip("\n")
    assert frame.endswith("\n\n"), "SSE 帧必须以空行结束，否则前端会把两帧粘成一帧"
    for line in body.split("\n"):
        if line.startswith("event: "):
            out["event"] = line[len("event: ") :]
        elif line.startswith("id: "):
            out["id"] = line[len("id: ") :]
        elif line.startswith("data: "):
            out["data"] = json.loads(line[len("data: ") :])
    return out


def test_format_sse_结构():
    frame = sse.format_sse("token", sse.envelope(200, "chunk", {"text": "你好"}), event_id="7")
    parsed = parse_frame(frame)
    assert parsed["event"] == "token" and parsed["id"] == "7"
    assert parsed["data"] == {"status": 200, "msg": "chunk", "data": {"text": "你好"}}


def test_format_sse_内容里的换行不会把帧切断():
    frame = sse.format_sse("token", sse.envelope(200, "chunk", {"text": "第一行\n第二行"}))
    assert frame.count("\n\n") == 1  # 只有结尾的分帧空行，正文换行被 JSON 转义了
    assert parse_frame(frame)["data"]["data"]["text"] == "第一行\n第二行"


def test_envelope_与_ApiResponse_同形():
    payload = sse.envelope(200, "done")
    assert set(payload) == {"status", "msg", "data"}
    assert payload["data"] == {}  # data 永远存在，前端解包不用判 None


def test_sse_headers_必须带反代不缓冲():
    headers = sse.sse_headers()
    assert headers["X-Accel-Buffering"] == "no"  # 少这个，nginx 会把整段答案攒完才发
    assert "no-cache" in headers["Cache-Control"]
    assert sse.sse_headers({"X-Extra": "1"})["X-Extra"] == "1"


def test_sse_response_是200且类型正确():
    async def _gen():
        yield sse.format_sse(sse.EVENT_DONE, sse.envelope(200, "done"))

    response = sse.sse_response(_gen())
    assert response.status_code == 200  # 非 200 会让 EventSource 直接断开并进入重连风暴
    assert response.media_type == "text/event-stream"


# --------------------------------------------------------------------------
# dict → 消息对象
# --------------------------------------------------------------------------


def test_assistant_历史不会被降级成用户发言():
    """原实现是 HumanMessage(**m)：role=assistant 也变成"用户说的话"，且不报错"""
    messages = sse.to_chat_messages([
        {"role": "system", "content": "约束"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "上一轮的回答"},
    ])
    assert [type(m) for m in messages] == [SystemMessage, HumanMessage, AIMessage]


# --------------------------------------------------------------------------
# chat model 路径
# --------------------------------------------------------------------------


class FakeChunkModel:
    """按给定文本逐块产出 AIMessageChunk

    不用 GenericFakeChatModel：它每次 astream 只产出一条**完整**消息（不是逐块），
    而且迭代器耗尽会抛 "No generation chunks were returned" —— 拿它验"逐字"会验错东西。
    """

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls: list[dict] = []

    async def astream(self, messages, **kwargs):
        self.calls.append(kwargs)
        for text in self.texts:
            yield AIMessageChunk(content=text)


def test_元组与已是消息对象的都接受():
    messages = sse.to_chat_messages([("system", "s"), AIMessage(content="a")])
    assert [type(m).__name__ for m in messages] == ["SystemMessage", "AIMessage"]


def test_缺_role_的dict兜成user而不是把流打断():
    """convert_to_messages 对 {"content": "..."} 会抛 ValueError，这里必须兜住"""
    messages = sse.to_chat_messages([{"content": "只给内容"}])
    assert type(messages[0]) is HumanMessage and messages[0].content == "只给内容"

def run_gen(agen) -> list[str]:
    async def _collect():
        return [frame async for frame in agen]

    return asyncio.run(_collect())


def test_chat_流逐字发token并在末尾发done():
    model = FakeChunkModel(["你", "好", "呀"])
    frames = run_gen(sse.stream_chat_tokens(model=model, messages=[{"role": "user", "content": "hi"}]))
    events = [parse_frame(f)["event"] for f in frames]
    assert events[0] == "meta" and events[-1] == "done"
    assert events.count("token") == 3
    done = parse_frame(frames[-1])["data"]["data"]
    assert done["text"] == "你好呀"  # done 带累计全文，前端刷新最终态不用自己拼


def test_不给_chat_model_传_stream_mode():
    """stream_mode 是 LangGraph 图的概念，传给 chat model 会透进厂商 SDK"""
    model = FakeChunkModel(["x"])
    run_gen(sse.stream_chat_tokens(model=model, messages=[{"role": "user", "content": "hi"}]))
    assert model.calls == [{}]


def test_references_帧在_done_之前():
    frames = run_gen(
        sse.stream_chat_tokens(
            model=FakeChunkModel(["答"]),
            messages=[("user", "q")],
            references=[{"document_id": "d1", "chunk_id": "c1"}],
        )
    )
    events = [parse_frame(f)["event"] for f in frames]
    assert events[-2:] == ["references", "done"]
    assert parse_frame(frames[-2])["data"]["data"]["items"][0]["document_id"] == "d1"


def test_出错帧不泄露异常原文():
    class BoomModel:
        async def astream(self, messages, **kwargs):
            yield AIMessage(content="已经说出去的一半")
            raise RuntimeError("upstream 401 api_key=sk-SECRET-abcdef host=10.0.0.7")

    frames = run_gen(sse.stream_chat_tokens(model=BoomModel(), messages=[("user", "q")]))
    blob = json.dumps(frames, ensure_ascii=False)
    assert "sk-SECRET-abcdef" not in blob and "10.0.0.7" not in blob
    error = parse_frame(frames[-1])
    assert error["event"] == "error"
    assert error["data"]["status"] == 500
    assert error["data"]["data"]["trace_id"]
    assert error["data"]["data"]["partial_text"] == "已经说出去的一半"  # 前端能保留已收到的内容


def test_error_之后不会再冒帧():
    """done/error 是终态帧，之后任何 yield 都是协议事故"""

    class HalfBoom:
        async def astream(self, messages, **kwargs):
            yield AIMessage(content="甲")
            raise ValueError("bad")

    frames = run_gen(sse.stream_chat_tokens(model=HalfBoom(), messages=[("user", "q")]))
    assert parse_frame(frames[-1])["event"] == "error"
    assert len(frames) == 3  # meta + token + error，没多出第二个终态帧


def test_心跳发的是注释帧不触发前端事件():
    class SlowModel:
        async def astream(self, messages, **kwargs):
            await asyncio.sleep(0.05)
            yield AIMessage(content="慢")

    frames = run_gen(
        sse.stream_chat_tokens(model=SlowModel(), messages=[("user", "q")], heartbeat_seconds=0.01)
    )
    assert any(f.startswith(":") for f in frames)
    assert parse_frame(frames[-1])["event"] == "done"


def test_空流也能正常收尾():
    class EmptyModel:
        async def astream(self, messages, **kwargs):
            return
            yield  # pragma: no cover

    frames = run_gen(sse.stream_chat_tokens(model=EmptyModel(), messages=[("user", "q")]))
    assert [parse_frame(f)["event"] for f in frames] == ["meta", "done"]
    assert parse_frame(frames[-1])["data"]["data"]["text"] == ""


def test_没有内容增量不发空token帧():
    frames = run_gen(sse.stream_chat_tokens(model=FakeChunkModel(["", "", "有"]), messages=[("user", "q")]))
    assert sum(1 for f in frames if parse_frame(f)["event"] == "token") == 1


# --------------------------------------------------------------------------
# agent（LangGraph）路径
# --------------------------------------------------------------------------


class StubAgent:
    """模拟 graph 的 stream_mode="messages"：每帧是 (message, metadata)"""

    def __init__(self, pieces, boom=False):
        self.pieces = pieces
        self.boom = boom
        self.seen_mode = None

    async def astream(self, messages, config=None, stream_mode=None):
        from langchain_core.messages import ToolMessage

        self.seen_mode = stream_mode
        for piece in self.pieces:
            if piece == "TOOL":  # 必须先判这个标记：它本身也是 str，顺序反了就被当成模型文本
                yield ToolMessage(content="工具原始输出", tool_call_id="t1"), {"langgraph_node": "tools"}
            elif isinstance(piece, str):
                yield AIMessage(content=piece), {"langgraph_node": "model"}
        if self.boom:
            raise RuntimeError("内部错误 不要外泄")


def test_agent_路径才用_stream_mode():
    agent = StubAgent(["你", "好"])
    frames = run_gen(sse.stream_agent_events(agent=agent, messages=[("user", "q")]))
    assert agent.seen_mode == "messages"
    assert [parse_frame(f)["event"] for f in frames] == ["meta", "token", "token", "done"]


def test_工具消息不当成模型说话流出去():
    agent = StubAgent(["甲", "TOOL", "乙"])
    frames = run_gen(sse.stream_agent_events(agent=agent, messages=[("user", "q")]))
    texts = [parse_frame(f)["data"]["data"]["text"] for f in frames if parse_frame(f)["event"] == "token"]
    assert texts == ["甲", "乙"]


def test_agent_出错同样不泄露():
    frames = run_gen(sse.stream_agent_events(agent=StubAgent(["甲"], boom=True), messages=[("user", "q")]))
    blob = json.dumps(frames, ensure_ascii=False)
    assert "内部错误" not in blob
    assert parse_frame(frames[-1])["event"] == "error"


# --------------------------------------------------------------------------
# 结构化输出契约
# --------------------------------------------------------------------------


def test_answer_为空时给出固定的不知道话术():
    assert AgentAnswer(answer="  ").answer == UNKNOWN_ANSWER_TEXT
    assert AgentAnswer(answer="正式回答").answerable is True


def test_sources_可以不填但类型要对():
    answer = AgentAnswer(answer="甲", sources=[{"document_id": "d1"}])
    assert answer.sources[0].document_id == "d1" and answer.sources[0].score is None


def test_score_保留四位且越界报错():
    ref = SourceRef(document_id="d1", score=0.7381926535)
    assert ref.score == 0.7382  # 展示位数固定，前端比较时不会因浮点尾巴出分歧
    with pytest.raises(ValidationError):
        SourceRef(document_id="d1", score=1.5)


def test_错误载荷里没有对外不该出现的字段():
    fields = set(StreamErrorData.model_fields)
    assert fields == {"trace_id", "retryable", "partial_text"}  # 故意没有 detail
    assert StreamErrorData(trace_id="abc12345").retryable is True


def test_response_format_与_bind_structured_对接正确():
    assert response_format() is AgentAnswer

    class Recorder:
        def __init__(self):
            self.bound = None

        def with_structured_output(self, schema, **kwargs):
            self.bound = schema
            return "bound"

    recorder = Recorder()
    assert bind_structured(recorder) == "bound"
    assert recorder.bound is AgentAnswer


def test_agent_模块不再顶层构造模型():
    """模块级 ChatOpenAI 会让占位 key 假装正常、真调用才炸；import 也可能被带崩"""
    import app.rag.agent as agent_mod

    assert not hasattr(agent_mod, "agent"), "模块级 agent 会强制要求 LLM key，别放回去"
    # 模块里可以有 llm（那是模块引用），但不能有已经构造好的聊天模型实例
    leaked = [name for name, value in vars(agent_mod).items() if isinstance(value, BaseChatModel)]
    assert leaked == []


def test_agent_未配key时明确报错而不是隐式失败(monkeypatch):
    from app.core.config import settings
    from app.rag.agent import build_agent
    from app.rag.memory.base import MemoryNotConfiguredError

    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    with pytest.raises(MemoryNotConfiguredError):
        build_agent()


def test_模板渲染三个占位符都在():
    from app.rag.agent import RAG_SYSTEM_PROMPT_TEMPLATE
    from app.rag.memory.context import safe_format

    rendered = safe_format(
        RAG_SYSTEM_PROMPT_TEMPLATE, {"context": "资料", "memory": "记忆", "question": "问题"}
    )
    for token in ("资料", "记忆", "问题"):
        assert token in rendered
    assert "{" not in rendered.replace("{context}", "").replace("}", "")  # 占位符都被替换掉了
