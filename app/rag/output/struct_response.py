"""结构化输出契约（给模型的答案必须长什么样）

先说清一个边界，能省掉后面所有的坑：**错误不要让模型来"结构化"**。
原来那份 `ErrorResponse` 是想让模型产出错误对象，但模型不知道自己什么时候失败 ——
它只会把失败也编成一段像模像样的 JSON。所以：
  - 业务/网络/校验错误 → 服务端产生，走统一异常处理器（HTTP）或 `error` 事件帧（SSE）；
  - 模型只负责一件事：**把答案和它的来源说清楚**，并且在资料不够时明确说"不知道"。

另外两个刻意的取舍：
  - 没有 `thinking` 字段。模型没有稳定可靠的思维链输出，强制要它写"思考过程"，
    得到的是事后编造的漂亮话：既漏内部实现，又白烧 token。要展示过程就用流式的中间帧。
  - `sources` 是**对象列表而不是字符串列表**。"使用了哪些数据源"这句话前端没法点 ——
    要能回到原文，就必须带 chunk_id / document_id，正好接 `/documents/detail|download`。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

UNKNOWN_ANSWER_TEXT = "参考资料里没有相关内容，我不能确定答案。"

MIN_SCORE = 0.0
MAX_SCORE = 1.0


class SourceRef(BaseModel):
    """一条引用：够前端渲染"来源卡片"并跳回原文"""

    model_config = ConfigDict(str_strip_whitespace=True)

    document_id: str = Field(description="文档ID，前端用它跳 /documents/detail")
    chunk_id: str | None = Field(default=None, description="命中的块ID")
    title: str = Field(default="", description="文档标题，展示用")
    snippet: str = Field(default="", description="命中的原文片段")
    page: int | None = Field(default=None, description="PDF 页码，docx/md 没有就是 None")
    heading: str | None = Field(default=None, description="命中标题路径")
    score: float | None = Field(default=None, ge=MIN_SCORE, le=MAX_SCORE, description="相关度 0~1")

    @field_validator("score", mode="before")
    @classmethod
    def _round_score(cls, value: Any) -> Any:
        """分数别给前端 0.7381926535 这种：展示位数固定，比较时也不会因浮点尾巴出分歧"""
        if value is None:
            return None
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return None


class AgentAnswer(BaseModel):
    """问答的结构化输出。`create_agent(response_format=AgentAnswer)` 直接用它"""

    model_config = ConfigDict(str_strip_whitespace=True)

    answer: str = Field(default="", description="面向用户的回答正文（中文，Markdown）")
    sources: list[SourceRef] = Field(default_factory=list, description="答案依据的资料来源")
    answerable: bool = Field(
        default=True,
        description="资料是否足够作答。false 时 answer 必须写明不知道，禁止编造",
    )
    kb_id: str | None = Field(default=None, description="本次作答用的知识库，便于前端与审计对上号")

    @field_validator("answer")
    @classmethod
    def _answer_not_blank(cls, value: str) -> str:
        # 资料不足时也要有话说，否则前端只能显示一个空气泡
        return value or UNKNOWN_ANSWER_TEXT


class StreamErrorData(BaseModel):
    """SSE `error` 帧的 data。**只放可公开的字段**

    `detail` 留给日志，不进响应：异常原文里常有 api_key、内网地址、厂商报文。
    """

    trace_id: str = Field(description="排错用的短随机串，用户报障时对着日志找")
    retryable: bool = Field(default=True, description="前端要不要显示[重试]按钮")
    partial_text: str = Field(default="", description="中断前已经流出去的内容，供前端保留")


def response_format() -> type[BaseModel]:
    """给 create_agent 用。留个函数而不是裸传类，方便以后按场景换 schema"""
    return AgentAnswer


def bind_structured(model, schema: type[BaseModel] | None = None):
    """不走 agent、只想一次性拿到 JSON 的场合（比如导出摘要、生成标签）

    注意：结构化输出和逐字流式**不要同时用**。走 ToolStrategy 时内容只在最后一个 tool_call
    里出现，前面全程没有可读文本，流式等于空转。要流式就让它自由作答、结束时单独发
    `references` 事件（见 sse.py 的 stream_chat_tokens）。
    """
    return model.with_structured_output(schema or AgentAnswer)


__all__ = [
    "AgentAnswer",
    "MAX_SCORE",
    "MIN_SCORE",
    "SourceRef",
    "StreamErrorData",
    "UNKNOWN_ANSWER_TEXT",
    "bind_structured",
    "response_format",
]
