"""切块：把解析出来的结构块切成能入库、能检索、能引用的单元

分工必须清楚，否则又要长出两套：
    `app/services/parsers/`  负责「把这个格式读成带结构的块」（page/heading/kind）
    本文件                    负责「把这些块切成合适长度的块」——**纯函数，不碰数据库、不碰网络**
    `app/services/process_document.py`  负责编排与写库

你原来那份 `app/rag/memory/splits.py` 做的正是这件事，但它自己又读了一遍文件
（txt/md/csv/json/log/pdf），于是和 parsers 的白名单长成两张皮：
上传收 docx/pptx 而它不读（这两个格式永远不会被入库），它还收 csv/json/log 而上传不收。
现在读取一律走 parsers/registry，切块只吃 ParsedBlock。

参数来源是 `knowledge_bases.chunk_size / chunk_overlap`（不是环境变量、也不是硬编码 1000/200），
这样"每个库不同切法"从 M0 建库接口就能配出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.parsers.base import ParsedBlock, ParsedDocument
from app.services.text_metrics import estimate_tokens

DEFAULT_CHUNK_SIZE = 800      # 和 knowledge_bases.chunk_size 的默认值保持一致
DEFAULT_CHUNK_OVERLAP = 120
MIN_PIECE_CHARS = 40          # 小于一句话的尾巴单独成块没有检索价值，并进上一块
BREAK_CHARS = "\n。！？；!?;. "  # 断点优先落在这里，别把句子腰斩


@dataclass(slots=True)
class ChunkPiece:
    """一块待入库的正文"""

    seq: int
    content: str
    token_count: int = 0
    page: int | None = None
    heading: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.token_count:
            self.token_count = estimate_tokens(self.content)


def _normalize_sizes(chunk_size: int | None, chunk_overlap: int | None) -> tuple[int, int]:
    """把非法参数收回到可用区间

    overlap >= chunk_size 会让窗口永远不前进（死循环），这不是"配置不合口味"，是事故，
    所以这里必须自己兜住而不是让调用方去保证。
    """
    size = DEFAULT_CHUNK_SIZE if not chunk_size or chunk_size <= 0 else int(chunk_size)
    overlap = DEFAULT_CHUNK_OVERLAP if chunk_overlap is None else int(chunk_overlap)
    if overlap < 0:
        overlap = 0
    if overlap >= size:
        overlap = size // 4
    return size, overlap


def _group_blocks(blocks: list[ParsedBlock]) -> list[tuple[str | None, int | None, list[str], list[str]]]:
    """按 (heading, page) 把相邻块归成一组

    heading 相同、页码也相同的块内容上是一段连续的，切在一组内部最自然；
    跨标题拼块会让检索命中的片段带着上一个标题的语境，引用定位就乱了。
    """
    groups: list[tuple[str | None, int | None, list[str], list[str]]] = []
    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        key_heading = block.heading or (text if block.kind == "heading" else None)
        if groups and groups[-1][0] == key_heading and groups[-1][1] == block.page:
            groups[-1][2].append(text)
            groups[-1][3].append(block.kind)
        else:
            groups.append((key_heading, block.page, [text], [block.kind]))
    return groups


def _split_text(text: str, size: int, overlap: int) -> list[str]:
    """一段文本切成若干块，尽量在句读处断开"""
    if len(text) <= size:
        return [text] if text.strip() else []
    pieces: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + size, total)
        if end < total:
            end = _soft_break(text, start, end)
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= total:
            break
        # 下一窗口的起点：留 overlap 的上下文，但要保证一定有前进量
        next_start = max(start + 1, end - overlap)
        start = next_start
    return _merge_small_tails(pieces, size)


def _soft_break(text: str, start: int, end: int) -> int:
    """从 end 往回找一个句子边界，最多回看 15% 的长度"""
    lookback = min(end - start - 1, max(16, (end - start) // 7))
    for offset in range(lookback + 1):
        position = end - offset
        if position <= start:
            break
        if text[position - 1] in BREAK_CHARS:
            return position
    return end


def _merge_small_tails(pieces: list[str], size: int) -> list[str]:
    """最后剩一小截就并进上一块，避免"半句话"单独成一个检索单元"""
    if len(pieces) >= 2 and len(pieces[-1]) < MIN_PIECE_CHARS and len(pieces[-2]) + len(pieces[-1]) <= size * 1.5:
        pieces[-2] = pieces[-2] + "\n" + pieces[-1]
        return pieces[:-1]
    return pieces


def chunk_blocks(
    blocks: list[ParsedBlock],
    *,
    chunk_size: int | None = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int | None = DEFAULT_CHUNK_OVERLAP,
) -> list[ChunkPiece]:
    """核心入口：结构块 → 切块结果（seq 从 0 连续，重跑结果一致）"""
    size, overlap = _normalize_sizes(chunk_size, chunk_overlap)
    pieces: list[ChunkPiece] = []
    for heading, page, texts, kinds in _group_blocks(blocks or []):
        joined = "\n\n".join(texts)
        for raw in _split_text(joined, size, overlap):
            pieces.append(
                ChunkPiece(
                    seq=len(pieces),
                    content=raw,
                    page=page,
                    heading=heading,
                    extra={"kinds": sorted(set(kinds)), "chunk_size": size, "chunk_overlap": overlap},
                )
            )
    return pieces


def chunk_parsed_document(
    parsed: ParsedDocument,
    *,
    chunk_size: int | None = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int | None = DEFAULT_CHUNK_OVERLAP,
) -> list[ChunkPiece]:
    """把 parsers 的产出直接切成块（编排里用的就是这个）"""
    return chunk_blocks(
        list(parsed.blocks),
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def total_tokens(pieces: list[ChunkPiece]) -> int:
    return sum(piece.token_count for piece in pieces)


__all__ = [
    "ChunkPiece",
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "chunk_blocks",
    "chunk_parsed_document",
    "total_tokens",
]
