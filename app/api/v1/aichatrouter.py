from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/aichat", tags=["AI Chat"])

# ==========================================
# 1. 会话管理 (Conversations)
# ==========================================

@router.post("/conversations")
async def create_conversation():
    """
    新建一个聊天会话。
    
    具体功能：
    - 接收前端传来的会话标题、关联的知识库 ID (kb_id)、以及选择的 Agent 类型。
    - 在数据库 `conversations` 表中新增一条记录。
    - 返回新建会话的唯一 ID (conversation_id)，前端后续发消息需携带此 ID。
    """
    pass


@router.get("/conversations")
async def list_conversations():
    """
    获取当前用户的会话列表。
    
    具体功能：
    - 根据当前登录用户的 ID 查询 `conversations` 表。
    - 按创建时间或最后更新时间倒序排列。
    - 返回给前端用于渲染左侧侧边栏的历史对话列表（包含标题、更新时间等）。
    """
    pass


@router.put("/conversations/{conversation_id}")
async def update_conversation():
    """
    更新（重命名）会话。
    
    具体功能：
    - 根据传入的 `conversation_id` 找到对应的会话记录。
    - 校验该会话是否属于当前登录用户（防止越权）。
    - 更新会话标题（例如用户手动修改，或由大模型根据首轮对话自动生成）。
    """
    pass


@router.delete("/conversations/{conversation_id}")
async def delete_conversation():
    """
    删除指定会话。
    
    具体功能：
    - 校验 `conversation_id` 归属权。
    - 从 `conversations` 表中删除该记录。
    - 级联删除该会话下的所有聊天消息记录（关联的 messages 或 chat_history 表）。
    """
    pass


# ==========================================
# 2. 消息与对话 (Messages & Chat)
# ==========================================

@router.get("/conversations/{conversation_id}/messages")
async def get_chat_history():
    """
    获取指定会话的历史消息记录。
    
    具体功能：
    - 校验会话归属权。
    - 查询该会话下的所有历史消息（按时间顺序排列）。
    - 返回包含用户提问、AI 回答以及引用文档元数据的消息列表，供前端渲染聊天气泡。
    """
    pass


@router.post("/conversations/{conversation_id}/chat")
async def chat_stream():
    """
    发送消息并进行流式问答 (SSE)。
    
    具体功能：
    - 接收用户输入的问题 (query)。
    - 将问题转化为向量，在 pgvector 中检索相关的文档切片（带用户和知识库隔离）。
    - 将检索到的上下文和问题组装成 Prompt，发送给大模型。
    - 以 Server-Sent Events (SSE) 的形式，逐字流式返回大模型的生成结果。
    - 在返回结束后，将完整的问答记录异步落库保存。
    """
    pass


@router.post("/conversations/{conversation_id}/stop")
async def stop_generation():
    """
    中断当前正在生成的回答。
    
    具体功能：
    - 前端用户点击“停止生成”按钮时调用。
    - 后端通过 asyncio 任务取消（Cancellation）或标志位，强制中断大模型的流式输出。
    - 停止计费，并将当前已生成的部分内容保存到数据库中。
    """
    pass


# ==========================================
# 3. Agent 与系统配置 (Agent Configs)
# ==========================================

@router.get("/agents")
async def list_available_agents():
    """
    获取可用的 Agent 列表。
    
    具体功能：
    - 返回系统当前配置的智能体列表（例如：“默认问答助手”、“代码专家”、“联网搜索”）。
    - 供前端在新建会话时提供下拉选择，决定使用哪种 RAG 策略或 Prompt 模板。
    """
    pass