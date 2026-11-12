"""短期记忆：单个会话内的上下文窗口

和长期记忆的分工，别混：
  短期 = "刚才聊到哪了" —— 只在一个 conversation 内有效，追求**便宜、即时、可控长度**
  长期 = "这个人/这件事值得一直记得" —— 跨会话，语义检索，只在相关时才进 prompt

要解决的问题很具体：多轮问答里历史消息会一直往 prompt 里堆，
不处理就是「token 越烧越多 → 超上下文报错 → 模型开始答非所问」。
这里的做法是标准的两段式：**滑动窗口保最近几轮 + 超预算把更老的压成摘要**。

三条不能省的设计决定：
  1. 会话缓存的 key 是 (user_id, conversation_id)，不是单 conversation_id。
     只按 conversation_id 索引，一旦有人猜到/拿到别人的 id，就能接着别人的话头问 —— 越权。
  2. 摘要不是"有 LLM 才压缩"。没有 key、调用失败也必须能压（规则式兜底），
     否则窗口会无限长，等于没做短期记忆。
  3. 进程内缓存 + 可选落盘。默认只在内存里（重启丢窗口是可以接受的，会话表以后进 DB），
     SHORT_TERM_PERSIST=True 才写 data/memory/sessions/，方便前端联调时不用一直重登重聊。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.rag import llm
from app.rag.memory.base import ChatRole, estimate_tokens, safe_segment, to_iso, utc_now

logger = logging.getLogger("app.rag.memory")

SESSION_SCHEMA_VERSION = 1
SYSTEM_ROLES = (ChatRole.SYSTEM, ChatRole.TOOL)


@dataclass(slots=True)
class ChatMessage:
    """一条消息。role 只用 user/assistant/system 三种，别把工具调用之类塞进来"""

    role: str
    content: str
    created_at: str = field(default_factory=lambda: to_iso(None))
    tokens: int = 0

    def __post_init__(self) -> None:
        self.role = str(self.role).strip().lower()
        self.content = "" if self.content is None else str(self.content)
        if not self.tokens:
            self.tokens = estimate_tokens(self.content)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content, "created_at": self.created_at, "tokens": self.tokens}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ChatMessage:
        return cls(
            role=str(raw.get("role") or "user"),
            content=str(raw.get("content") or ""),
            created_at=str(raw.get("created_at") or to_iso(None)),
            tokens=int(raw.get("tokens") or 0),
        )


@dataclass(slots=True)
class SessionState:
    """一个会话的短期记忆全量状态"""

    conversation_id: str
    user_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    summary: str = ""
    total_turns: int = 0          # 累计轮数（压缩不会让它变小，给前端显示"第几轮"用）
    compressed_turns: int = 0     # 已经被压进摘要的轮数
    updated_at: str = field(default_factory=lambda: to_iso(None))

    @property
    def window_tokens(self) -> int:
        return sum(message.tokens for message in self.messages) + estimate_tokens(self.summary)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "summary": self.summary,
            "total_turns": self.total_turns,
            "compressed_turns": self.compressed_turns,
            "updated_at": self.updated_at,
            "messages": [message.to_dict() for message in self.messages],
        }


Summarizer = Callable[[str, Sequence[ChatMessage], int], str]


def rule_compress(previous_summary: str, dropped: Sequence[ChatMessage], max_chars: int) -> str:
    """没有 LLM 也能压缩：老问题的开头 + 已有摘要，截断到预算内

    质量当然不如模型摘要，但"能压"比"压得好"重要 —— 压不动就等于窗口无限膨胀。
    """
    questions = [
        " ".join(message.content.split())[:60]
        for message in dropped
        if message.role == ChatRole.USER
    ]
    parts = [segment for segment in ([previous_summary.strip()] + questions) if segment]
    return "；".join(parts)[-max_chars:] if parts else ""


def model_compress(previous_summary: str, dropped: Sequence[ChatMessage], max_chars: int) -> str:
    """用模型把溢出的一批消息并进已有摘要；失败自动退回规则版"""
    transcript = "\n".join(f"{'用户' if m.role == ChatRole.USER else '助手'}：{m.content}" for m in dropped)
    prompt = (
        f"已有摘要：{previous_summary or '（无）'}\n需要并入的新内容：\n{transcript}\n"
        f"请输出合并后的摘要，不超过 {max_chars} 字，只输出正文。"
    )
    text = llm.complete(prompt, system_prompt=SUMMARY_RULE, temperature=0.0, max_chars=max_chars * 3)
    if not text or estimate_tokens(text) > max_chars * 1.5:
        return rule_compress(previous_summary, dropped, max_chars)
    return text[: max_chars * 2]


SUMMARY_RULE = "你是对话压缩器，把历史对话合并成简短要点摘要，保留目标、结论和未解决的问题。"


class ShortTermMemory:
    """进程内会话窗口缓存（可选落盘）

    线程安全：所有公开方法都在一把 RLock 里；PGVector/文件 I/O 都是同步的，
    从异步路由调用请走 context.py 里的 a* 包装（丢线程池），不要在事件循环里直接跑。
    """

    def __init__(
        self,
        *,
        max_turns: int | None = None,
        token_budget: int | None = None,
        ttl_seconds: int | None = None,
        max_sessions: int | None = None,
        persist_dir: str | Path | None = None,
        summary_max_chars: int | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.max_turns = settings.SHORT_TERM_MAX_TURNS if max_turns is None else max_turns
        self.token_budget = settings.SHORT_TERM_TOKEN_BUDGET if token_budget is None else token_budget
        self.ttl_seconds = settings.SHORT_TERM_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self.max_sessions = settings.SHORT_TERM_MAX_SESSIONS if max_sessions is None else max_sessions
        self.summary_max_chars = (
            settings.SHORT_TERM_SUMMARY_MAX_CHARS if summary_max_chars is None else summary_max_chars
        )
        self.persist_dir = Path(persist_dir) if persist_dir else None
        if self.persist_dir is None and settings.SHORT_TERM_PERSIST:
            self.persist_dir = Path(settings.MEMORY_DIR) / "sessions"
        self._summarizer = summarizer or self._auto_summarizer
        self._sessions: OrderedDict[tuple[str, str], SessionState] = OrderedDict()
        self._lock = threading.RLock()

    # ---------- 配置与后端选择 ----------
    @staticmethod
    def _auto_summarizer(previous_summary: str, dropped: Sequence[ChatMessage], max_chars: int) -> str:
        """配了真 key 就用模型压缩，否则规则版 —— 两条路都必须返回可用的文本"""
        if settings.llm_ready:
            return model_compress(previous_summary, dropped, max_chars)
        return rule_compress(previous_summary, dropped, max_chars)

    @property
    def enabled(self) -> bool:
        return bool(settings.SHORT_TERM_ENABLED)

    @staticmethod
    def _key(conversation_id: str, user_id: str) -> tuple[str, str]:
        # 两个都必须有值：user_id 缺省就报错，不做"默认用户"，防止跨用户串会话
        if not user_id:
            raise ValueError("短期记忆必须带 user_id（会话窗口按 (user_id, conversation_id) 隔离）")
        if not conversation_id:
            raise ValueError("短期记忆必须带 conversation_id")
        return str(user_id), str(conversation_id)

    # ---------- 磁盘 ----------
    def _path(self, user_id: str, conversation_id: str) -> Path | None:
        if not self.persist_dir:
            return None
        return self.persist_dir / f"{safe_segment(user_id, 'user')}__{safe_segment(conversation_id, 'conv')}.json"

    def _read_disk(self, user_id: str, conversation_id: str) -> SessionState | None:
        path = self._path(user_id, conversation_id)
        if not path or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if int(raw.get("schema_version") or 0) != SESSION_SCHEMA_VERSION:
                return None  # 结构变了就当作没有，不猜旧格式
            state = SessionState(
                conversation_id=str(raw.get("conversation_id") or conversation_id),
                user_id=str(raw.get("user_id") or user_id),
                summary=str(raw.get("summary") or ""),
                total_turns=int(raw.get("total_turns") or 0),
                compressed_turns=int(raw.get("compressed_turns") or 0),
                updated_at=str(raw.get("updated_at") or to_iso(None)),
                messages=[ChatMessage.from_dict(item) for item in raw.get("messages") or [] if isinstance(item, dict)],
            )
            return state
        except Exception as exc:  # noqa: BLE001 —— 文件坏了宁可当没有，也不能让整个问答接口挂掉
            logger.warning("会话文件读不了，按空会话继续: %s: %s", type(exc).__name__, str(exc)[:160])
            return None

    def _write_disk(self, state: SessionState) -> None:
        path = self._path(state.user_id, state.conversation_id)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话落盘失败（内存窗口仍然有效）: %s: %s", type(exc).__name__, str(exc)[:160])

    # ---------- 核心：取状态（含 TTL / LRU / 冷加载） ----------
    def state(self, conversation_id: str, user_id: str) -> SessionState:
        uid, cid = self._key(conversation_id, user_id)
        key = (uid, cid)
        with self._lock:
            cached = self._sessions.get(key)
            if cached is not None:
                if self._expired(cached):
                    self._sessions.pop(key, None)
                    cached = None
                else:
                    self._sessions.move_to_end(key)
                    return cached
            # 缓存里没有就冷加载（落盘过的会话重启后还能接上），再放回缓存
            loaded = self._read_disk(uid, cid) or SessionState(conversation_id=cid, user_id=uid)
            self._sessions[key] = loaded
            self._evict_if_needed()
            return loaded

    def _expired(self, state: SessionState) -> bool:
        if not self.ttl_seconds:
            return False
        try:
            touched = datetime.fromisoformat(state.updated_at)
        except ValueError:
            return False
        return (utc_now() - touched).total_seconds() > self.ttl_seconds

    def _evict_if_needed(self) -> None:
        """缓存满了：最久没用的那个先出，出之前已经落过盘就不丢数据"""
        if not self.max_sessions or self.max_sessions < 0:
            return
        while len(self._sessions) > self.max_sessions:
            key, old = self._sessions.popitem(last=False)
            if self.persist_dir:
                self._write_disk(old)

    def _touch(self, state: SessionState) -> None:
        state.updated_at = to_iso(None)
        if self.persist_dir:
            self._write_disk(state)

    # ---------- 写 ----------
    def append(self, conversation_id: str, user_id: str, role: str, content: str) -> ChatMessage:
        message = ChatMessage(role=role, content=content)
        if not message.content.strip():
            return message
        state = self.state(conversation_id, user_id)
        with self._lock:
            state.messages.append(message)
            if message.role == ChatRole.USER:
                state.total_turns += 1
            self._compress(state)
            self._touch(state)
        return message

    def add_turn(self, conversation_id: str, user_id: str, user_content: str, assistant_content: str) -> None:
        """一轮 = 一问一答。压缩按轮为单位，不会出现"只有问没有答"的半轮"""
        state = self.state(conversation_id, user_id)
        with self._lock:
            for role, content in ((ChatRole.USER, user_content), (ChatRole.ASSISTANT, assistant_content)):
                message = ChatMessage(role=role, content=content)
                if message.content.strip():
                    state.messages.append(message)
            state.total_turns += 1
            self._compress(state)
            self._touch(state)

    # ---------- 压缩 ----------
    def _over_limit(self, state: SessionState) -> bool:
        if self.max_turns and len(state.messages) > self.max_turns * 2:
            return True
        return bool(self.token_budget and state.window_tokens > self.token_budget)

    def _compress(self, state: SessionState) -> None:
        """把最老的整轮挪出窗口、并进 summary

        保留至少一轮：窗口压到空是 bug，模型会失去当前问题的语境（用户会觉得"它把我上一句忘了"）。
        """
        guard = 0
        while self._over_limit(state) and len(state.messages) > 2 and guard < 50:
            guard += 1
            pair = state.messages[:2]
            del state.messages[:2]
            state.compressed_turns += 1
            state.summary = self._summarizer(state.summary, pair, self.summary_max_chars)

    # ---------- 读 ----------
    def window(self, conversation_id: str, user_id: str) -> list[ChatMessage]:
        state = self.state(conversation_id, user_id)
        return [
            ChatMessage.from_dict(message.to_dict())
            for message in state.messages
            if message.role not in SYSTEM_ROLES
        ]

    def summary(self, conversation_id: str, user_id: str) -> str:
        return self.state(conversation_id, user_id).summary

    def messages(self, conversation_id: str, user_id: str, *, include_summary: bool = True) -> list[dict[str, str]]:
        """给模型的历史部分：摘要（作为 system 说明）在前，最近若干轮在后"""
        state = self.state(conversation_id, user_id)
        out: list[dict[str, str]] = []
        if include_summary and state.summary.strip():
            out.append({"role": "system", "content": f"（更早的对话已压缩为摘要）{state.summary}"})
        out.extend({"role": message.role, "content": message.content} for message in state.messages)
        return out

    def clear(self, conversation_id: str, user_id: str) -> bool:
        uid, cid = self._key(conversation_id, user_id)
        with self._lock:
            existed = self._sessions.pop((uid, cid), None) is not None
            path = self._path(uid, cid)
            if path and path.exists():
                path.unlink()
                existed = True
        return existed

    def stats(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        state = self.state(conversation_id, user_id)
        return {
            "conversation_id": state.conversation_id,
            "messages": len(state.messages),
            "total_turns": state.total_turns,
            "compressed_turns": state.compressed_turns,
            "window_tokens": state.window_tokens,
            "summary_chars": len(state.summary),
            "max_turns": self.max_turns,
            "token_budget": self.token_budget,
            "persisted": bool(self.persist_dir),
            "updated_at": state.updated_at,
        }

    def cached_sessions(self) -> int:
        with self._lock:
            return len(self._sessions)

    def forget_user(self, user_id: str) -> int:
        """注销账号 / 数据清理时用：把这个用户的会话窗口全抹掉（含落盘文件）"""
        uid = str(user_id)
        with self._lock:
            keys = [key for key in self._sessions if key[0] == uid]
            for key in keys:
                self._sessions.pop(key, None)
        removed = len(keys)
        if self.persist_dir and self.persist_dir.exists():
            for path in self.persist_dir.glob(f"{safe_segment(uid, 'user')}__*.json"):
                path.unlink(missing_ok=True)
                removed += 1
        return removed


@lru_cache(maxsize=1)
def get_short_term_memory() -> ShortTermMemory:
    """进程内单例：窗口缓存必须是共享的，每请求 new 一个等于每次都是新会话"""
    return ShortTermMemory()


def reset_short_term_memory() -> None:
    """改了配置、或测试之间要彻底隔离时用（缓存里的窗口会一起丢）"""
    get_short_term_memory.cache_clear()
