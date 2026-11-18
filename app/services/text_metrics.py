"""文本长度/Token 估算

放在 services 层是因为**两层记忆和切块都要用它**，而它没有任何业务含义：
    - `app/services/chunker.py`：按长度切块、统计 chunks.token_count
    - `app/rag/memory/session.py`：短期窗口的 token 预算
    - `app/rag/memory/context.py`：估算这轮 prompt 大概多少 token

为什么不用 tiktoken：首次调用要联网下载词表，离线机器和 CI 上会直接卡住。
这里的口径是**估算**（中文 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token），
用途是"预算控制"和"给前端看个大概"，不是计费依据 —— 需要精确值时
把 settings.SHORT_TERM_USE_TIKTOKEN 打开，并且**先在能联网的环境跑过一次**让词表落地。
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[㐀-䶿一-鿿　-〿＀-￯-]")


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (max(1, other // 4) if other else 0)


def char_len(text: str | None) -> int:
    return len(text or "")


__all__ = ["estimate_tokens", "char_len"]
