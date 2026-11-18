"""AI 对话路由（M5：会话管理 + 问答）

对应表：chat_sessions（会话头）+ chat_messages（消息）。
你原来注释里写的 conversations / messages 两张表就是它们，命名按现有 models 走。

八条路由的分工：
    POST   /conversations                     新建会话（可限定某个知识库）
    GET    /conversations                     会话列表（分页 + 标题搜索）
    PUT    /conversations/{conversation_id}   重命名
    DELETE /conversations/{conversation_id}   删除（消息级联删）
    GET    /conversations/{conversation_id}/messages   历史（游标翻页）
    POST   /conversations/{conversation_id}/chat       问答：SSE 流式，或 stream=false 出 JSON
    POST   /conversations/{conversation_id}/stop       停止生成
    GET    /agents                            可选 agent 列表（M8 前是配置常量）

三条全局约定：
  1. **身份只来自 get_current_user**，任何入参里都没有 user_id；会话/消息的每条查询
     都带 user_id（见 chat_crud 的模块说明），别人的 id 在这里就是 404，而不是 403
     ——403 等于告诉对方"这个会话存在"。
  2. **路由要薄**（DEVELOPMENT §2）：检索、prompt 拼装、模型调用、落库全在
     app/rag/answer.py + app/rag/chat.py 里，这里只做取参 → 调用 → 组装 ApiResponse → 决定状态码。
  3. **流式的错误分两段**：开始推流之前（会话不存在、没配模型、该会话已有回答在生成）
     走正常的 4xx/5xx JSON；开始推流之后只能 200 + error 帧（§3.6），
     所以 build_turn 在返回 StreamingResponse 之前调用，而不是在生成器里面。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import DB, CurrentUser, SessionFactory
from app.core.config import settings
from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.db.crud import chat_crud
from app.rag import answer as answer_service
from app.rag import chat as chat_service
from app.rag.generation import get_registry
from app.rag.memory.base import MemoryNotConfiguredError
from app.rag.output.sse import sse_response
from app.schemas.chat_schemas import (
    HISTORY_MAX_LIMIT,
    AgentOut,
    ChatAnswerOut,
    ChatAskRequest,
    ChatCreate,
    ChatHistoryOut,
    ChatOut,
    ChatStopOut,
    ChatUpdate,
    MessageOut,
)
from app.schemas.common import ApiResponse, PageData, PageParams
from app.services import kb_access

logger = logging.getLogger("app.api.aichat")

router = APIRouter(prefix="/aichat", tags=["AI Chat"])  # 版本前缀只在 main.py 出现一次

CONVERSATION_NOT_FOUND = "会话不存在"

# M8 之前这份目录是常量（前端下拉用它）：available 只反映"当前配置能不能跑"，
# 真正的 agent 组装在 app/rag/agent.py:build_agent()
AGENT_CATALOG: tuple[dict, ...] = (
    {
        "key": "rag_default",
        "name": "知识库问答",
        "description": "按你有权限的知识库检索资料后作答，并给出引用出处（默认）",
        "needs_kb": False,
        "supports_stream": True,
    },
    {
        "key": "rag_kb_only",
        "name": "仅库内作答",
        "description": "资料里没有就直接说不知道，不做任何推断",
        "needs_kb": True,
        "supports_stream": True,
    },
    {
        "key": "agent_tools",
        "name": "工具型 Agent",
        "description": "多轮工具调用（检索 + 统计 + 联网），需要 langchain-deepseek 依赖",
        "needs_kb": False,
        "supports_stream": False,
    },
)


def _conversation_or_404(conversation_id, current_user, db):
    """所有按 conversation_id 操作的接口共用这一道归属校验"""
    return chat_service.get_chat(conversation_id, current_user.id, db)


# ==========================================
# 1. 会话管理
# ==========================================


@router.post("/conversations", response_model=ApiResponse[ChatOut], summary="新建会话")
async def create_conversation(
    payload: ChatCreate,
    current_user: CurrentUser,
    db: DB,
):
    """新建一个会话，返回 conversation_id

    落地：
        1. kb_id 传了就先过权限（读不到 → 404）。不查的话外键失败只会得到一个笼统的 500，
           用户根本不知道是自己没权限还是库被删了；
        2. 标题留空用「新对话」，首轮问答会自动改成问题前缀（chat 接口的 auto_title）；
        3. agent 选择要等 M8 给 chat_sessions 加列，现在传了也没地方存。
    """
    if payload.kb_id is not None:
        access = await kb_access.access_of(payload.kb_id, current_user.id, db)
        if access is None or not access.can_read:
            raise NotFoundError("知识库不存在或你没有读取权限")

    session = await chat_service.new_chat(current_user.id, db, title=payload.title, kb_id=payload.kb_id)
    if session is None:
        raise BusinessError("创建会话失败，请稍后重试", status=500)
    return ApiResponse.ok(ChatOut.from_session(session), msg="会话已创建")


@router.get("/conversations", response_model=ApiResponse[PageData[ChatOut]], summary="会话列表")
async def list_conversations(
    current_user: CurrentUser,
    db: DB,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 20,
    keyword: Annotated[str | None, Query(max_length=100, description="按标题模糊搜索")] = None,
):
    """当前用户的会话列表，按最后一次发言时间倒序

    落地：
        1. 只查自己的（chat_crud 里 user_id 是硬条件，不靠"前端别传别人的 id"）；
        2. 排序用 last_message_at 而不是 created_at —— 用户找的是"刚才聊的那个"；
        3. 不返回摘要正文，只返回有没有摘要（内部材料，见 chat_schemas 模块说明）。
    """
    params = PageParams(page=page, size=size)
    items, total = await chat_service.list_chats(
        current_user.id, db, offset=params.offset, limit=params.limit, keyword=keyword
    )
    data = PageData[ChatOut](
        items=[ChatOut.from_session(item) for item in items],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ApiResponse.ok(data, msg="获取成功")


@router.put("/conversations/{conversation_id}", response_model=ApiResponse[ChatOut], summary="重命名会话")
async def update_conversation(
    payload: ChatUpdate,
    current_user: CurrentUser,
    db: DB,
    conversation_id: Annotated[uuid.UUID, Path(description="会话ID")],
):
    """改标题（校验归属）

    换知识库范围不提供接口：那是会话的检索边界，中途改会让历史消息和后来的回答
    对不上出处，需要换库就新建会话。
    """
    session = await _conversation_or_404(conversation_id, current_user, db)
    if session is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)

    updated = await chat_service.rename_chat(conversation_id, current_user.id, payload.title, db)
    if updated is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)
    return ApiResponse.ok(ChatOut.from_session(updated), msg="修改成功")


@router.delete("/conversations/{conversation_id}", response_model=ApiResponse[None], summary="删除会话")
async def delete_conversation(
    current_user: CurrentUser,
    db: DB,
    conversation_id: Annotated[uuid.UUID, Path(description="会话ID")],
):
    """删除会话及其全部消息

    落地：
        1. 先取消该会话正在进行的生成：不然删完之后流还在往外吐字，
           收尾落库会撞上"会话没了"（消息插不进去，白烧一次 token）；
        2. 消息由外键 ON DELETE CASCADE 清理（模型里 passive_deletes=True，
           不会先把几百条消息查进内存再逐条删）；
        3. **长期记忆不删**：那是用户级的事实（"用户负责财务报销"），
           不属于某个会话，删会话不应该顺手把人家的偏好抹掉。要清记忆走 /user 那边的注销逻辑。
    """
    session = await _conversation_or_404(conversation_id, current_user, db)
    if session is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)

    get_registry().cancel(current_user.id, conversation_id)

    if not await chat_service.delete_chat(conversation_id, current_user.id, db):
        raise NotFoundError(CONVERSATION_NOT_FOUND)
    return ApiResponse.ok(msg="删除成功")


# ==========================================
# 2. 历史消息
# ==========================================


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ApiResponse[ChatHistoryOut],
    summary="会话历史消息",
)
async def get_chat_history(
    current_user: CurrentUser,
    db: DB,
    conversation_id: Annotated[uuid.UUID, Path(description="会话ID")],
    limit: Annotated[int, Query(ge=1, le=HISTORY_MAX_LIMIT, description="取多少条")] = 50,
    before_seq: Annotated[
        int | None, Query(ge=1, description="只取这个序号之前的（向上翻更早的消息）")
    ] = None,
):
    """按 seq 升序返回一页消息 + 有没有更早的

    落地：
        1. 游标翻页（before_seq）而不是 page/size：聊天是无限上翻的场景，
           偏移分页在有新消息插入时会让页码整体错位、同一条被读两遍；
        2. has_more 用「本页最早一条的 seq 是否 > 1」判断，比 count 少一次查询且不会因为
           并发写入而算错；
        3. 用户消息和助手消息都返回，前端按 role 决定气泡方向；sources 就是引用出处。
    """
    session = await _conversation_or_404(conversation_id, current_user, db)
    if session is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)

    rows = await chat_service.read_history(conversation_id, current_user.id, db, limit=limit, before_seq=before_seq)
    if rows is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)

    total = await chat_crud.count_messages(conversation_id, current_user.id, db)
    oldest_seq = int(rows[0].seq) if rows else None
    data = ChatHistoryOut(
        session=ChatOut.from_session(session),
        items=[MessageOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        before_seq=before_seq,
        has_more=bool(oldest_seq and oldest_seq > 1),
    )
    return ApiResponse.ok(data, msg="获取成功")


# ==========================================
# 3. 问答
# ==========================================


@router.post(
    "/conversations/{conversation_id}/chat",
    response_class=StreamingResponse,
    summary="发送消息并获取回答（默认 SSE 流式）",
)
async def chat_stream(
    payload: ChatAskRequest,
    current_user: CurrentUser,
    db: DB,
    session_factory: SessionFactory,
    conversation_id: Annotated[uuid.UUID, Path(description="会话ID")],
):
    """一轮问答：检索 → 拼上下文 → 模型 → 边生成边发 → 落库

    帧序列（stream=true，详见 DEVELOPMENT §3.6）：
        meta        一次：session_id / model / 检索方式与命中数 / generation_id
        token       多次：{"text": 增量}
        references  最多一次：引用出处列表（前端做角标和"点回原文"）
        done        一次：全文收尾
        error       出错时替换 done：只给 trace_id，异常原文进日志

    落地：
        1. build_turn 在**返回响应之前**跑完（会话归属、知识库范围、检索、prompt 拼装、
           模型可用性）：这些失败必须是正常 HTTP 状态码，流一旦开始就只能 200；
        2. 同一会话同时只允许一路生成 → 409。手机用户手快点两次发送，
           会烧两份 token 并让 seq 抢跑；
        3. 落库用 session_factory 新开的 Session（理由写在 app/rag/answer.py 顶部）；
        4. stream=false 走同一条链路，只是把 token 攒完整体返回 JSON ——
           脚本、CI 冒烟、以及不支持 SSE 的客户端用它。
    """
    # 模型拿不到（没配 key / 占位 key）是**可预期的配置问题** → 503 + 明确提示；
    # 其它异常（数据库挂了、检索 SQL 出错）不许冒充模型问题，原样交给全局 500 处理器，
    # 否则运维照着"检查 LLM_API_KEY"排半小时才发现是 PG 没起来。
    try:
        plan = await answer_service.build_turn(
            conversation_id, current_user.id, payload.question, db, top_k=payload.top_k
        )
    except MemoryNotConfiguredError as exc:
        logger.warning("模型未配置，无法回答 session=%s: %s", conversation_id, str(exc)[:200])
        raise BusinessError("未配置大模型或模型不可用，暂时无法生成回答", status=503) from exc

    if plan is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)

    if not payload.stream:
        try:
            result = await answer_service.answer_turn(
                plan, session_factory=session_factory, write_memory=payload.write_memory, auto_title=payload.auto_title
            )
        except answer_service.AnswerError as exc:
            raise BusinessError(exc.msg, status=503, data={"trace_id": exc.trace_id, "retryable": True}) from exc
        out = ChatAnswerOut.model_validate(result)
        return JSONResponse(
            status_code=200,
            content=ApiResponse[ChatAnswerOut].ok(out, msg="回答完成").model_dump(mode="json"),
        )

    generation = get_registry().acquire(current_user.id, conversation_id)
    if generation is None:
        raise ConflictError("这个会话已经有一条回答正在生成，请等它结束或先点停止")

    return sse_response(
        answer_service.stream_turn(
            plan,
            session_factory=session_factory,
            generation=generation,
            write_memory=payload.write_memory,
            auto_title=payload.auto_title,
        )
    )


@router.post(
    "/conversations/{conversation_id}/stop",
    response_model=ApiResponse[ChatStopOut],
    summary="停止生成",
)
async def stop_generation(
    current_user: CurrentUser,
    db: DB,
    conversation_id: Annotated[uuid.UUID, Path(description="会话ID")],
):
    """请求中断当前正在生成的回答

    落地：
        1. 只置取消标志，不 asyncio.Task.cancel()：已生成的半截答案要能落库，
           流也要发出正常的 done 帧，否则前端会显示"回答中断"而不是用户实际看到的文字；
        2. **没有在跑的东西不算错误**：返回 200 + stopped=false。手机用户点停止时，
           流往往刚好结束，这时报错只会让前端多写一个特殊分支；
        3. 局限：注册表是进程内的，多 worker 部署时 stop 请求必须落到同一个进程才有效
           （排期见 TODO M9：粘性连接或 Redis 广播）。
    """
    session = await _conversation_or_404(conversation_id, current_user, db)
    if session is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)

    generation = get_registry().cancel(current_user.id, conversation_id)
    if generation is None:
        data = ChatStopOut(session_id=conversation_id, stopped=False)
        return ApiResponse.ok(data, msg="当前没有正在生成的回答")

    data = ChatStopOut(
        session_id=conversation_id,
        stopped=True,
        generation_id=generation.generation_id,
        partial_chars=generation.emitted_chars,
    )
    return ApiResponse.ok(data, msg="已停止，保留已生成的内容")


# ==========================================
# 4. Agent 目录
# ==========================================


@router.get("/agents", response_model=ApiResponse[list[AgentOut]], summary="可用 Agent 列表")
async def list_available_agents(current_user: CurrentUser):
    """返回前端新建会话时的下拉列表

    available 反映当前配置能不能真跑（没配 LLM key 就全是 false，并给可直接展示的中文原因），
    比"接口 200 但一点发送就 500"友好得多。

    要登录：目录里带的是本部署启用了哪些能力，属于配置信息，不对匿名访客开放。
    """
    ready = settings.llm_ready
    reason = None if ready else "未配置大模型 API Key，请先在 .env 里填 LLM_API_KEY"
    items = [
        AgentOut(
            key=item["key"],
            name=item["name"],
            description=item["description"],
            needs_kb=bool(item["needs_kb"]),
            supports_stream=bool(item["supports_stream"]),
            available=ready if item["key"] != "agent_tools" else False,
            unavailable_reason=reason if item["key"] != "agent_tools" else "工具型 Agent 需要额外依赖，M8 接入",
        )
        for item in AGENT_CATALOG
    ]
    return ApiResponse.ok(items, msg="获取成功")

