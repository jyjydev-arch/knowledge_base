"""PDF 解析器（pypdf）

按页出块，页码写进 ParsedBlock.page —— 回答里标「第 3 页」全靠它。
"""

from pathlib import Path

from app.services.parsers.base import DocumentParseError, ParsedBlock, ParsedDocument, clean_text

# 少于这个字符量就认为「没有可提取文本」：一页正文通常远多于 8 个字符，
# 阈值太低会把只有页眉页脚的页误判成空文档，太高会漏掉真正的短文档。
_MIN_TEXT_PER_PAGE = 8
_MIN_TOTAL_TEXT = 16


def parse(path: Path) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 依赖没装时给可操作的提示，而不是裸 ImportError
        raise DocumentParseError(f"缺少 pypdf 依赖: {exc}") from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise DocumentParseError(f"PDF 无法打开（可能已损坏）: {exc}") from exc

    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise DocumentParseError("PDF 已加密，请导出无密码版本后再上传")
        except Exception as exc:
            raise DocumentParseError(f"PDF 已加密，无法解析: {exc}") from exc

    blocks: list[ParsedBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = clean_text(page.extract_text() or "")
        except Exception as exc:
            # 单页解析失败不该让整篇失败：记下来继续，末尾再判空
            blocks.append(ParsedBlock(text="", page=page_number, kind="text", extra={"error": str(exc)[:200]}))
            continue
        if len(text) >= _MIN_TEXT_PER_PAGE:
            blocks.append(ParsedBlock(text=text, page=page_number))

    total_chars = sum(len(block.text) for block in blocks)
    if total_chars < _MIN_TOTAL_TEXT:
        raise DocumentParseError(
            "没有从 PDF 中提取到文字，可能是扫描件或图片型 PDF（需要 OCR，当前版本未支持）"
        )

    info = reader.metadata or {}
    return ParsedDocument(
        blocks=blocks,
        meta={
            "format": "pdf",
            "page_count": len(reader.pages),
            "title": str(info.title) if getattr(info, "title", None) else None,
            "author": str(info.author) if getattr(info, "author", None) else None,
        },
    )
