"""PowerPoint (.pptx) 解析器（python-pptx）

一页幻灯片一个块，page = 幻灯片序号；备注（notes）单独成块，
因为讲稿里往往才是真正有信息量的正文，而幻灯片上只有关键词。
"""

from pathlib import Path

from app.services.parsers.base import DocumentParseError, ParsedBlock, ParsedDocument, clean_text


def parse(path: Path) -> ParsedDocument:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise DocumentParseError(f"缺少 python-pptx 依赖: {exc}") from exc

    try:
        presentation = Presentation(str(path))
    except Exception as exc:
        raise DocumentParseError(f"pptx 无法打开（.ppt 老格式或文件损坏？只支持 .pptx）: {exc}") from exc

    blocks: list[ParsedBlock] = []
    for slide_number, slide in enumerate(presentation.slides, start=1):
        title, texts, tables = _collect_shapes(slide)
        heading = clean_text(title) or None

        body = "\n\n".join(clean_text(text) for text in texts if clean_text(text))
        if body:
            blocks.append(ParsedBlock(text=body, page=slide_number, heading=heading,
                                      extra={"slide": slide_number}))

        for table_index, table in enumerate(tables, start=1):
            table_text = _table_text(table)
            if table_text:
                blocks.append(ParsedBlock(text=table_text, page=slide_number, heading=heading, kind="table",
                                          extra={"slide": slide_number, "table": table_index}))

        notes = _notes_text(slide)
        if notes:
            # 备注标 kind=notes，切块时可以按类型降权或单独索引
            blocks.append(ParsedBlock(text=notes, page=slide_number, heading=heading, kind="notes",
                                      extra={"slide": slide_number}))

    document = ParsedDocument(
        blocks=blocks,
        meta={"format": "pptx", "slide_count": len(presentation.slides)},
    )
    if document.is_empty():
        raise DocumentParseError("pptx 没有可提取的文字内容（可能是纯图片幻灯片）")
    return document


def _collect_shapes(slide) -> tuple[str | None, list[str], list]:
    """递归取文本框与表格（组合图形里还嵌着 shape，所以必须递归）"""
    title: str | None = None
    texts: list[str] = []
    tables: list = []

    for shape in slide.shapes:
        if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
            sub_title, sub_texts, sub_tables = _collect_shapes(shape)
            title = title or sub_title
            texts.extend(sub_texts)
            tables.extend(sub_tables)
            continue

        if getattr(shape, "has_table", False):
            tables.append(shape.table)
            continue

        if getattr(shape, "has_text_frame", False):
            text = "\n".join(paragraph.text for paragraph in shape.text_frame.paragraphs)
            if not clean_text(text):
                continue
            if title is None and shape == slide.shapes.title:
                title = text            # 标题作为这一页的 heading，不进正文
            else:
                texts.append(text)

    return title, texts, tables


def _notes_text(slide) -> str:
    if not slide.has_notes_slide:
        return ""
    return clean_text(slide.notes_slide.notes_text_frame.text)


def _table_text(table) -> str:
    rows = []
    for row in table.rows:
        cells = [clean_text(cell.text) for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)
