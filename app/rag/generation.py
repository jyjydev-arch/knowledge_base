"""正在生成的回答：进程内注册表（POST /aichat/conversations/{id}/stop 的实现基础）

为什么用进程内而不是数据库：
  「停一下」是**毫秒级**的交互，写库轮询要么慢要么得额外开一条 SSE 通道；
  而生成任务本来就活在某个 worker 进程的 asyncio.Task 里，取消它只能在那个进程内做。

必须知道的局限（不是 bug，是当前架构的边界）：
  1. **多 worker 部署时 stop 只在同一个进程有效**。gunicorn/uvicorn --workers N 会把
     同一个用户的两次请求分到不同进程，注册表互不可见。真上多 worker 得换 Redis pub/sub
     或每 worker 一个粘性连接（排在 M9，别现在假装已解决）。
  2. 客户端断开（手机切后台）不经过这里：那条流是靠 SSE 生成器被取消自然结束的，
     注册表里的记录由 finally 释放。
  3. 只存"哪路生成在跑 + 一个取消标志"，**不缓存正文**：正文由生成器自己累积，
     这里放一份就会有两份真相。

key 用 f"{user_id}:{conversation_id}"：与 LangGraph checkpointer 的 thread_id 同口径，
少一个字段就可能允许 A 用户停掉 B 用户的生成。
"""

from __future__ import annotations

import asyncio
import time
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any


def thread_key(user_id: Any, conversation_id: Any) -> str:
    """会话在进程内的唯一键。和 app/rag/memory 里 thread_id 的拼法保持一致"""
    return f"{user_id}:{conversation_id}"


@dataclass
class ActiveGeneration:
    """一次正在进行的生成"""

    user_id: str
    conversation_id: str
    generation_id: str = field(default_factory=lambda: uuid_lib.uuid4().hex)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)
    emitted_chars: int = 0

    @property
    def key(self) -> str:
        return thread_key(self.user_id, self.conversation_id)

    @property
    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def cancel(self) -> bool:
        """置取消标志。返回是否"由未取消变成取消"，让 /stop 能区分首次和重复点击"""
        if self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        return True

    def add_chars(self, count: int) -> None:
        self.emitted_chars = int(self.emitted_chars) + int(count)


class GenerationRegistry:
    """进程内的生成注册表。同一会话同时只允许一路生成（409 由路由决定）"""

    def __init__(self) -> None:
        self._active: dict[str, ActiveGeneration] = {}

    def acquire(self, user_id: Any, conversation_id: Any) -> ActiveGeneration | None:
        """登记一路生成；该会话已有在跑的则返回 None（让路由报 409，而不是偷偷并行两路）

        僵尸自愈：上一路流如果被硬杀（进程重启、异常没走到 finally），键会一直占着。
        这里靠 started_at 判断，超过 CHAT_STALE_SECONDS 的旧记录直接顶掉。
        """
        key = thread_key(user_id, conversation_id)
        existing = self._active.get(key)
        if existing is not None:
            from app.core.config import settings

            if existing.elapsed_seconds < settings.CHAT_STALE_GENERATION_SECONDS:
                return None
            existing.cancel()
        generation = ActiveGeneration(user_id=str(user_id), conversation_id=str(conversation_id))
        self._active[key] = generation
        return generation

    def get(self, user_id: Any, conversation_id: Any) -> ActiveGeneration | None:
        return self._active.get(thread_key(user_id, conversation_id))

    def cancel(self, user_id: Any, conversation_id: Any) -> ActiveGeneration | None:
        """请求停止。返回被停掉的那一路（没有在跑的返回 None）"""
        generation = self.get(user_id, conversation_id)
        if generation is None:
            return None
        generation.cancel()
        return generation

    def release(self, generation: ActiveGeneration) -> None:
        """收尾注销。必须比对对象而不是键 ——
        否则上一路流的 finally 会把"已经顶替它的新那一轮"从表里删掉。"""
        current = self._active.get(generation.key)
        if current is generation:
            self._active.pop(generation.key, None)

    def count(self, user_id: Any | None = None) -> int:
        if user_id is None:
            return len(self._active)
        return sum(1 for item in self._active.values() if item.user_id == str(user_id))

    def clear(self) -> None:
        """测试/进程内重置用。生产代码不要调（会把在跑的流变成"没人管的孤儿键"）"""
        self._active.clear()


_registry = GenerationRegistry()


def get_registry() -> GenerationRegistry:
    return _registry


def reset_registry() -> None:
    """测试夹具用：用例之间不能共享"某会话正在生成"的状态"""
    _registry.clear()


__all__ = ["ActiveGeneration", "GenerationRegistry", "get_registry", "reset_registry", "thread_key"]
