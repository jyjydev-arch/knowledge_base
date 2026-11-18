"""会话与问答（/api/v1/aichat）的请求 / 响应模型

命名沿用 DEVELOPMENT §3.7：XxxCreate / XxxUpdate / XxxOut。

三个刻意的决定：
1. **不外泄 summary 正文**。chat_sessions.summary 是发给模型的内部材料（早期对话压缩结果，
   里面可能出现别人文档的片段），前端只需要"有没有摘要 / 多长"来判断要不要显示折叠提示。
2. **conversation_id 用 uuid 类型**。路径参数交给 FastAPI 校验，非 uuid 直接 422，
   不进业务层（crud 里另有 _to_uuid 兜底，两层）。
3. **sources 用 dict 而不是强模型**。它是 JSON 列，字段随检索方式变化（关键词 / 向量 / 以后的联网），
   前端按键取用即可；加字段不用改接口版本。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 与会话表默认标题保持一致：首轮问答靠它判断"这个会话还没命名过"
DEFAULT_CHAT_TITLE = "新对话"
TITLE_MAX_LEN = 255
QUESTION_MAX_LEN = 6000  # 单轮问题上限：再长的内容应该传成文档，而不是塞进 prompt
HISTORY_MAX_LIMIT = 200  # 一次最多取多少条历史（移动端首屏只要最近几轮）


class ChatCreate(BaseModel):
    """新建会话"""

    title: str | None = Field(default=None, max_length=TITLE_MAX_LEN, description="会话标题，留空则用「新对话」")
    kb_id: uuid.UUID | None = Field(
        default=None,
        description="限定的知识库范围（必须是你读得到的库）；留空=在你所有可读库+长期记忆里检索",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


class ChatUpdate(BaseModel):
    """重命名会话。只支持改标题——换 kb_id 等于换检索范围，请新建会话"""

    title: str = Field(min_length=1, max_length=TITLE_MAX_LEN, description="新的会话标题")

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("标题不能是空白字符")
        return value


class ChatOut(BaseModel):
    """会话头部（列表和详情共用）"""

    id: uuid.UUID = Field(description="会话ID（前端叫 conversation_id）")
    kb_id: uuid.UUID | None = Field(default=None, description="限定的知识库；删库后为 null（会话保留）")
    title: str = Field(description="会话标题")
    message_count: int = Field(default=0, description="已入库消息条数（用户+助手都算）")
    token_count: int = Field(default=0, description="累计 token 估算值")
    has_summary: bool = Field(default=False, description="早期对话是否已压缩成摘要")
    summary_chars: int = Field(default=0, description="摘要长度（字符）。正文不返回，那是模型内部材料")
    last_message_at: datetime | None = Field(default=None, description="最后一次发言时间，未发言为 null")
    created_at: datetime | None = Field(default=None, description="创建时间")

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_session(cls, session) -> "ChatOut":
        """ORM → 出参。summary 只给长度（见模块 docstring 第 1 条）"""
        summary = getattr(session, "summary", "") or ""
        return cls(
            id=session.id,
            kb_id=session.kb_id,
            title=session.title,
            message_count=int(session.message_count or 0),
            token_count=int(session.token_count or 0),
            has_summary=bool(summary.strip()),
            summary_chars=len(summary),
            last_message_at=session.last_message_at,
            created_at=session.created_at,
        )


class MessageOut(BaseModel):
    """一条消息（聊天气泡）"""

    id: uuid.UUID = Field(description="消息ID")
    session_id: uuid.UUID = Field(description="所属会话ID")
    seq: int = Field(description="会话内序号，从 1 起；翻页/续播都靠它")
    role: str = Field(description="user / assistant")
    content: str = Field(description="正文")
    token_count: int = Field(default=0, description="这条消息的 token 估算值")
    sources: list[dict] = Field(default_factory=list, description="引用来源，用户消息为空数组")
    created_at: datetime | None = Field(default=None, description="时间")

    model_config = ConfigDict(from_attributes=True)


class ChatHistoryOut(BaseModel):
    """历史消息（游标翻页，不用 page/size —— 聊天流只能"再往前翻一页"）

    items 始终按 seq 升序（正着读），before_seq 是"我读到了第几号"，
    has_more=true 时把本页第一条的 seq 当作下次请求的 before_seq。
    """

    session: ChatOut = Field(description="会话头部，省得前端为标题再请求一次")
    items: list[MessageOut] = Field(default_factory=list, description="消息，按 seq 升序")
    total: int = Field(default=0, description="该会话已入库的消息总数")
    limit: int = Field(default=0, description="本次请求的条数上限")
    before_seq: int | None = Field(default=None, description="本次请求的游标（null=取最新一页）")
    has_more: bool = Field(default=False, description="是否还有更早的消息没读")


class ChatAskRequest(BaseModel):
    """发一条消息

    注意没有 user_id：身份只认 Authorization（get_current_user），
    请求体里能传 user_id 就等于允许冒充。
    """

    question: str = Field(min_length=1, max_length=QUESTION_MAX_LEN, description="用户的问题")
    stream: bool = Field(default=True, description="true=SSE 逐字流；false=一次性 JSON（脚本/调试用）")
    top_k: int | None = Field(default=None, ge=1, le=10, description="本次检索条数，留空用配置默认")
    write_memory: bool | None = Field(
        default=None, description="本轮是否落长期记忆；留空按 MEMORY_AUTO_WRITE，传 false 可临时关掉"
    )
    auto_title: bool = Field(default=True, description="首轮问答时是否用问题自动命名会话")

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空")
        return value


class ChatAnswerOut(BaseModel):
    """stream=false 的返回体（stream=true 时这些内容走 SSE 的 references/done 帧）"""

    session_id: uuid.UUID = Field(description="会话ID")
    answer: str = Field(description="模型回答全文")
    sources: list[dict] = Field(default_factory=list, description="引用来源（可能为空数组）")
    retrieval_mode: str = Field(description="检索车道: keyword / disabled / no_scope / none")
    stopped: bool = Field(default=False, description="是否被 /stop 中断（非流式车道恒为 false）")
    turn: dict = Field(default_factory=dict, description="本轮落库结果：消息数、窗口 token 等")


class ChatStopOut(BaseModel):
    """停止生成的结果"""

    session_id: uuid.UUID = Field(description="会话ID")
    stopped: bool = Field(description="是否真的停掉了一个在跑的回答；没有在跑的也是 200 + false")
    generation_id: str | None = Field(default=None, description="被停掉的那次生成的 ID")
    partial_chars: int = Field(default=0, description="停止时已经生成的字符数（便于前端判断要不要保留）")


class AgentOut(BaseModel):
    """一个可选的问答 agent（M8 之前这张表是配置里的常量，不入库）"""

    key: str = Field(description="会话创建时要传的 agent 标识（M8 起生效）")
    name: str = Field(description="展示名")
    description: str = Field(description="一句话说明")
    needs_kb: bool = Field(default=False, description="是否必须选定知识库才能用")
    supports_stream: bool = Field(default=True, description="是否支持逐字流式")
    available: bool = Field(description="当前配置下能不能用")
    unavailable_reason: str | None = Field(default=None, description="不可用的原因，可直接展示给用户")


__all__ = [
    "AgentOut",
    "ChatAnswerOut",
    "ChatAskRequest",
    "ChatCreate",
    "ChatHistoryOut",
    "ChatOut",
    "ChatStopOut",
    "ChatUpdate",
    "DEFAULT_CHAT_TITLE",
    "HISTORY_MAX_LIMIT",
    "MessageOut",
    "QUESTION_MAX_LEN",
    "TITLE_MAX_LEN",
]
