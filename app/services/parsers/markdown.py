"""Markdown / 纯文本解析器

不引第三方 markdown 库：我们要的是「纯文本 + 结构」，转 HTML 反而没用，
自己按标题层级切更可控，也顺带覆盖 .txt（没有标题语法时整篇按段落切）。
"""

import re
from pathlib import Path

from app.services.parsers.base import DocumentParseError, ParsedBlock, ParsedDocument, clean_text

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_SETEXT_HEADING = re.compile(r"^[=\-]{3,}\s*$")
_FRONT_MATTER_DELIM = re.compile(r"^---\s*$")
_CODE_FENCE = re.compile(r"^(```|~~~)")


def parse(path: Path) -> ParsedDocument:
    """按标题分节切块，每块记住它所属的标题路径（H1 / H2 / H3）

    这样后面检索命中一块时，模型能知道这段内容在文档里的位置，回答里的引用才有意义。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentParseError(f"不是 UTF-8 文本文件: {exc}") from exc

    lines = raw.split("\n")
    blocks: list[ParsedBlock] = []
    heading_path: list[str] = []      # 当前标题栈，深度 = 级别
    buffer: list[str] = []
    in_code = False
    in_front_matter = False
    meta: dict = {}
    lineno = 0

    def flush() -> None:
        text = clean_text("\n".join(buffer))
        buffer.clear()
        if not text:
            return
        blocks.append(
            ParsedBlock(
                text=text,
                heading=" / ".join(heading_path) if heading_path else None,
                extra={"start_line": lineno},
            )
        )

    for index, line in enumerate(lines):
        lineno = index + 1
        stripped = line.strip()

        if index == 0 and _FRONT_MATTER_DELIM.match(stripped):
            in_front_matter = True
            continue
        if in_front_matter:
            if _FRONT_MATTER_DELIM.match(stripped):
                in_front_matter = False
            elif ":" in stripped:
                key, _, value = stripped.partition(":")
                meta[key.strip()] = value.strip()
            continue

        if _CODE_FENCE.match(stripped):
            in_code = not in_code
            buffer.append(line)
            continue

        if not in_code:
            level = _heading_level(lines, index, stripped)
            if level:
                atx = _ATX_HEADING.match(stripped)
                if atx:
                    title = atx.group(2).strip()
                    flush()
                else:
                    # Setext 写法（标题下面一行写 === / ---）：标题文字在上一行，
                    # 得先从缓冲区里把它取出来当标题，否则会把上一段正文当成标题内容
                    while buffer and not buffer[-1].strip():
                        buffer.pop()
                    title = clean_text(buffer[-1]) if buffer else ""
                    if not title:
                        continue
                    buffer.pop()
                heading_path = heading_path[: level - 1] + [title]
                blocks.append(ParsedBlock(text=title, heading=" / ".join(heading_path), kind="heading",
                                          extra={"level": level}))
                continue

        buffer.append(line)

    flush()

    doc = ParsedDocument(blocks=blocks, meta={**meta, "format": "markdown", "line_count": len(lines)})
    if doc.is_empty():
        raise DocumentParseError("文档没有可提取的正文内容")
    return doc


def _heading_level(lines: list[str], index: int, stripped: str) -> int | None:
    """识别 ATX（# 标题）和 Setext（标题下面写 === / ---）两种写法"""
    atx = _ATX_HEADING.match(stripped)
    if atx:
        return len(atx.group(1))
    if _SETEXT_HEADING.match(stripped) and index > 0:
        previous = lines[index - 1].strip()
        if previous and not previous.startswith(("#", "*", "-", "!", "`", ">")) and not _CODE_FENCE.match(previous):
            return 1 if "=" in stripped[0] else 2
    return None
