"""Word (.docx) 解析器（python-docx）

按 body 顺序遍历段落与表格（document.paragraphs 只有段落，会丢掉表格内容，
所以必须走 element.body 迭代），用样式名识别标题层级，维护标题路径。
"""

import re
from pathlib import Path

from app.services.parsers.base import DocumentParseError, ParsedBlock, ParsedDocument, clean_text

_HEADING_STYLE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.IGNORECASE)
# docx 里没有可靠页码（分页是渲染期算的），所以 page 一律留 None，出处用标题路径 + 段落序号


def parse(path: Path) -> ParsedDocument:
    try:
        from docx import Document as DocxDocument
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DocumentParseError(f"缺少 python-docx 依赖: {exc}") from exc

    try:
        doc = DocxDocument(str(path))
    except Exception as exc:
        raise DocumentParseError(f"docx 无法打开（.doc 老格式或文件损坏？只支持 .docx）: {exc}") from exc

    heading_path: list[str] = []
    blocks: list[ParsedBlock] = []
    index = 0

    for child in doc.element.body:
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, doc)
            text = clean_text(paragraph.text)
            if not text:
                continue
            index += 1
            level = _heading_level(paragraph)
            if level:
                heading_path = heading_path[: level - 1] + [text]
                blocks.append(
                    ParsedBlock(text=text, heading=" / ".join(heading_path), kind="heading",
                                extra={"level": level, "index": index})
                )
            else:
                blocks.append(
                    ParsedBlock(text=text, heading=" / ".join(heading_path) or None,
                                extra={"index": index, "style": paragraph.style.name if paragraph.style else None})
                )
        elif child.tag == qn("w:tbl"):
            index += 1
            table_text = _table_text(Table(child, doc))
            if table_text:
                # 表格转成「表头 | 值」一行行，比塞 HTML 好检索，也方便模型理解
                blocks.append(
                    ParsedBlock(text=table_text, heading=" / ".join(heading_path) or None, kind="table",
                                extra={"index": index})
                )

    doc_meta = doc.core_properties
    document = ParsedDocument(
        blocks=blocks,
        meta={
            "format": "docx",
            "title": doc_meta.title or None,
            "author": doc_meta.author or None,
            "block_count": index,
        },
    )
    if document.is_empty():
        raise DocumentParseError("docx 没有可提取的正文内容")
    return document


def _heading_level(paragraph) -> int | None:
    style = paragraph.style.name if paragraph.style else ""
    match = _HEADING_STYLE.match((style or "").strip())
    if match:
        return int(match.group(1))
    # 英文文档常见 Title 样式当一级标题用
    if style.strip().lower() == "title":
        return 1
    return None


def _table_text(table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [clean_text(cell.text) for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)
