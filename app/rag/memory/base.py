"""记忆层的数据结构与后端接口

两层记忆，各管各的事：
  短期（app/rag/memory/session.py）＝ 这个会话里刚才说了什么 → 窗口 + 超预算压缩
  长期（本文件 + local/pgvector 两个后端）＝ 跨会话还值得记住什么 → 按 user_id 隔离的语义检索

后端只有三个实现，换哪一个都不影响上层：
  local    → 本地 JSONL + 词面 hash 向量。开发/测试车道：不联网、不建 PG、不装 pgvector 也能跑
  pgvector → langchain_postgres.PGVector。生产车道：语义检索，需要 pgvector 扩展 + embedding 服务
  off      → 全链路 no-op（灰度、或不想让历史进 prompt 时）

约定：
  - 接口一律同步（PGVector 本身就是同步实现）。异步路由里要放线程池调用，
    app/rag/memory/context.py 里的 a* 函数已经包好了，不要在协程里直接跑重活。
  - 记忆层属于 service，不抛 HTTP 异常；只抛本文件的 MemoryError 家族，由路由决定提示语。
  - user_id 一律当不可信输入处理（要落文件名的地方先 sanitize）。
"""

from __future__ import annotations

import math
import re
import uuid as uuid_lib
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ChatRole(StrEnum):
    """会话消息的角色。只有这三种会进短期记忆窗口

    单独列出来是因为 role 会被原样拼进发给模型的 messages，
    传错值（比如前端直接写 system 注入指令）在问答场景是真问题。
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


# 记忆条目类型：conversation/summary 是自动落的，fact/preference 是抽取出来的"值得长期记的事"
MEMORY_TYPES = ("conversation", "summary", "fact", "preference", "note")

_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.\-]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


class MemoryError(Exception):
    """记忆层的基类异常"""


class MemoryNotConfiguredError(MemoryError):
    """缺依赖：比如 pgvector 后端但 DATABASE_URL 是 sqlite、或没配 embedding key"""


class BackendCapabilityError(MemoryError):
    """某个后端不支持的操作（例如 PG 后端拿不到稳定的时间排序时）"""


def utc_now() -> datetime:
    """统一用带时区的 UTC，落库/落盘都存 ISO 字符串，避免本地时区歧义"""
    return datetime.now(UTC)


def to_iso(dt: datetime | None) -> str:
    """保留微秒。只到秒的话，同一秒写入的多条记忆时间戳完全相同，
    "按最旧淘汰"就会挑错人（真实事故：把最新的三条当最旧的删了）。"""
    return (dt or utc_now()).astimezone(UTC).isoformat()


def safe_segment(value: Any, name: str = "user_id") -> str:
    """把 user_id / conversation_id 收成能当文件名的安全片段

    这两个值最终会拼进 data/memory/... 的路径里，必须防目录穿越（`../../etc/passwd`）。
    全被过滤掉（例如纯中文用户名）时用 sha1 短摘要兜底，保证不同输入不会撞成同一个文件。
    """
    raw = "" if value is None else str(value).strip()
    cleaned = _SEGMENT_RE.sub("_", raw)[:80].strip("._")
    if cleaned:
        return cleaned
    digest = __import__("hashlib").sha1(raw.encode("utf-8")).hexdigest()
    return f"{name}_{digest[:16]}"


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数，用于短期记忆的预算控制

    为什么不直接用 tiktoken：首次调用要联网下载词表，离线/CI 会卡。
    估算口径：中日韩字符 ≈ 1 token/字，其余 ≈ 4 字符/1 token。
    真要精确计数就把 SHORT_TERM_USE_TIKTOKEN 设 True（模型词表下载过才有效）。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk


@dataclass(slots=True)
class MemoryItem:
    """一条长期记忆

    score 只有检索结果才填，写入时为 None；这样同一个结构既能当存储模型也能当结果模型。
    """

    text: str
    user_id: str
    memory_type: str = "conversation"
    created_at: str = field(default_factory=lambda: to_iso(None))
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid_lib.uuid4()))
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "user_id": self.user_id,
            "memory_type": self.memory_type,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MemoryItem:
        return cls(
            id=str(raw.get("id") or uuid_lib.uuid4()),
            text=str(raw.get("text") or ""),
            user_id=str(raw.get("user_id") or ""),
            memory_type=str(raw.get("memory_type") or "conversation"),
            created_at=str(raw.get("created_at") or to_iso(None)),
            metadata=dict(raw.get("metadata") or {}),
            score=raw.get("score"),
        )


class MemoryBackend(ABC):
    """长期记忆后端接口

    实现要点：所有方法都必须按 user_id 硬隔离。跨用户读到别人的记忆是这类系统最严重的洞，
    所以接口里没有「不带 user_id 的 search」，想全库排查请直接用后端自己的工具。
    """

    name = "abstract"
    # 相似度分布是后端属性：语义向量（pgvector）随便一个弱相关也有 0.4+，
    # 词面 hash（local）强相关才 0.4 左右。所以「配置里的阈值」要按后端的 scale 折算，
    # 让 MEMORY_SCORE_THRESHOLD=0.5 在两个后端上都表示"中等相关"，而不是各自调一个数。
    default_threshold = 0.5
    threshold_scale = 1.0

    def scale_threshold(self, configured: float | None) -> float:
        """把配置里的通用阈值折算成当前后端的口径；传 None 就用后端默认值"""
        base_value = self.default_threshold if configured is None else float(configured)
        return round(base_value * self.threshold_scale, 4)

    @abstractmethod
    def add(self, items: Sequence[MemoryItem], dedupe_threshold: float | None = None) -> list[str]:
        """写入若干条，返回真正落库的 id（被去重丢掉的不在里面）

        dedupe_threshold 传 None 用后端/配置的默认口径；显式传值用于"这次就是要严格判重"。
        """

    @abstractmethod
    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
        threshold: float | None = None,
        memory_type: str | None = None,
    ) -> list[MemoryItem]:
        """语义检索：按相似度降序，低于 threshold 的直接不要

        threshold 传 None = 用后端自己的默认口径（各后端相似度分布不同，见 threshold_scale）。
        """

    def list_items(self, user_id: str, limit: int = 50, offset: int = 0) -> list[MemoryItem]:
        """按时间倒序列出该用户的记忆（给管理界面/调试用）"""
        raise BackendCapabilityError(f"{self.name} 后端不支持列举记忆")

    def count(self, user_id: str) -> int:
        raise BackendCapabilityError(f"{self.name} 后端不支持统计记忆")

    def delete(self, user_id: str, memory_ids: Sequence[str]) -> int:
        raise BackendCapabilityError(f"{self.name} 后端不支持删除记忆")

    def prune(self, user_id: str, max_items: int) -> int:
        """容量兜底：超过 max_items 就淘汰最旧的。默认不实现的后端直接返回 0（不阻塞写入）"""
        try:
            total = self.count(user_id)
        except BackendCapabilityError:
            return 0
        if total <= max_items:
            return 0
        stale = self.list_items(user_id, limit=total - max_items, offset=max_items)
        return self.delete(user_id, [item.id for item in stale])

    def close(self) -> None:  # noqa: B027
        """释放连接池之类的资源，默认不做任何事"""


class NullBackend(MemoryBackend):
    """MEMORY_BACKEND=off 时用：接口照旧调用，但什么都不存

    留着这个实现，比在业务代码里到处写 `if settings.MEMORY_BACKEND != "off"` 干净。
    """

    name = "off"

    def add(self, items: Sequence[MemoryItem], dedupe_threshold: float | None = None) -> list[str]:
        return []

    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
        threshold: float | None = None,
        memory_type: str | None = None,
    ) -> list[MemoryItem]:
        return []

    def list_items(self, user_id: str, limit: int = 50, offset: int = 0) -> list[MemoryItem]:
        return []

    def count(self, user_id: str) -> int:
        return 0

    def delete(self, user_id: str, memory_ids: Sequence[str]) -> int:
        return 0


def cosine(a: dict[int, float], b: dict[int, float]) -> float:
    """稀疏向量余弦相似度（local 后端用得上；两个都是 L2 归一化后的，点积即余弦）"""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(dim, 0.0) for dim, weight in a.items())


def normalize_vector(vector: dict[int, float]) -> dict[int, float]:
    norm = math.sqrt(sum(w * w for w in vector.values()))
    if norm == 0:
        return {}
    return {dim: w / norm for dim, w in vector.items()}
