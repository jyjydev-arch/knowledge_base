"""LLM 调用的唯一入口（记忆层和以后的问答都用这里）

为什么要单独一层：
  - ChatOpenAI 在没配 api_key 时构造就抛异常；如果在模块顶层 new，
    整个包 import 就炸（agent.py 现在就卡在这上面）。这里全部延迟到真正要调用时。
  - 记忆抽取/摘要这类活儿「调不通也必须能继续跑」：没有 key、超时、返回垃圾，
    都降级成规则式结果并返回空串或规则摘要，而不是把异常抛穿到请求里。
  - 模型名/端点/温度只从 settings 读，不散落 os.environ.get。
  - 占位 key（your_api_key_here 这种）由 settings.llm_ready 判为未配置，
    不然开发期每个请求都会真去远端拿一次 401：慢、吵、还容易误判成模型坏了。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import settings
from app.rag.memory.base import MemoryNotConfiguredError, estimate_tokens

logger = logging.getLogger("app.rag")

FACTS_SYSTEM_PROMPT = (
    "你是记忆抽取器。只从下面这轮对话中提取【用户本人的长期事实、偏好、约束】，"
    "每条一行，不要编号、不要解释、不要推测。没有值得长期记住的内容就只输出 NONE。"
)
SUMMARY_SYSTEM_PROMPT = (
    "你是对话压缩器。把下面的历史对话压成不超过 {max_chars} 字的要点摘要，"
    "保留：用户的目标、已确认的结论、仍未解决的问题。只输出摘要正文。"
)


@lru_cache(maxsize=8)
def _chat_model(model: str, temperature: float):
    """按 (model, temperature) 缓存客户端；连接池在客户端内部，别每轮 new 一个"""
    from langchain_openai import ChatOpenAI

    if not settings.llm_ready:
        raise MemoryNotConfiguredError(
            "未配置 LLM_API_KEY：请在 .env 里填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL"
        )
    return ChatOpenAI(
        model=model,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=temperature,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=1,
    )


def get_chat_model(model: str | None = None, temperature: float | None = None):
    return _chat_model(model or settings.LLM_MODEL, settings.LLM_TEMPERATURE if temperature is None else temperature)


def complete(user_prompt: str, system_prompt: str = "", *, model: str | None = None,
             temperature: float | None = None, max_chars: int = 1500) -> str:
    """一次性补全。任何失败都返回空串：调用方决定怎么降级，异常不往请求外抛"""
    if not settings.llm_ready:
        return ""
    try:
        client = get_chat_model(model=model, temperature=temperature)
        messages = ([("system", system_prompt)] if system_prompt else []) + [("human", user_prompt)]
        reply = client.invoke(messages)
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        return text.strip()[:max_chars]
    except Exception as exc:  # noqa: BLE001 —— 网络/额度/格式问题一律降级，不能拖垮问答接口
        logger.warning("LLM 调用失败，降级处理: %s: %s", type(exc).__name__, str(exc)[:200])
        return ""


def extract_facts(user_input: str, assistant_reply: str) -> list[str]:
    """抽取「值得长期记住的用户事实」；LLM 不可用或判定没内容时返回空列表"""
    raw = complete(
        f"用户：{user_input}\n助手：{assistant_reply}",
        FACTS_SYSTEM_PROMPT,
        temperature=0.0,
        max_chars=800,
    )
    if not raw or raw.strip().upper() == "NONE":
        return []
    facts: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*•·0123456789. ").strip()
        if line and line.upper() != "NONE":
            facts.append(line[:200])
    return facts[:5]  # 一轮最多记 5 条，防止模型话痨把记忆灌满


def rule_summary(user_input: str, assistant_reply: str, max_chars: int = 240) -> str:
    """规则式摘要：只留用户问题的开头 + 回答的结论句，不联网也能跑"""
    question = " ".join(user_input.split())[: max_chars // 2]
    answer_head = " ".join(assistant_reply.split())[: max_chars // 3]
    return f"问：{question}；答：{answer_head}"[:max_chars]


def summarize_turn(user_input: str, assistant_reply: str, max_chars: int = 200) -> str:
    """把一轮问答压成短句，作为长期记忆的 conversation/summary 条目"""
    text = complete(
        f"用户：{user_input}\n助手：{assistant_reply}",
        SUMMARY_SYSTEM_PROMPT.format(max_chars=max_chars),
        temperature=0.0,
        max_chars=max_chars * 3,
    )
    if not text or estimate_tokens(text) > max_chars * 1.5:
        return rule_summary(user_input, assistant_reply, max_chars)
    return text


def reset_llm_cache() -> None:
    """改了配置（换模型/换 key）之后清掉缓存的客户端"""
    _chat_model.cache_clear()
