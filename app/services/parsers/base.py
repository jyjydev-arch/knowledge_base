"""文档解析器的公共约定

M2（解析 → 切块 → 入库）里，这层只负责一件事：
    把一个二进制/文本文件读成「带结构的纯文本块」，不碰数据库、不抛 HTTP 异常。

统一产出 ParsedDocument.blocks，每块带 page / heading / kind，
后面切块时可以直接按 heading 保留上下文、按 page 记录出处（回答时好标引用）。
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

# 连续空白压缩：\xc2\xa0 是 PDF/docx 里常见的 no-break space，不当空白处理会留下一堆奇怪空格
_MULTI_BLANK = re.compile(r"[ \t\u00a0]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


class DocumentParseError(Exception):
    """解析失败（格式不支持、文件损坏、加密、没有可提取文本等）

    service 层不碰 HTTP（见 DEVELOPMENT.md），由调用方（路由/M2 编排）决定
    转成哪种 status/msg：不支持的格式 → 400，文件坏了 → 422，加密 → 400 提示导出纯文本。
    """


class UnsupportedFormatError(DocumentParseError):
    """扩展名不在支持列表里"""


@dataclass
class ParsedBlock:
    """一个结构块：一段正文、一个表格或一页幻灯片"""

    text: str
    page: int | None = None            # pdf=页码，pptx=页（幻灯片序号），docx=None（该格式没有稳定页码）
    heading: str | None = None         # 所属标题（多级时用 " / " 连接）
    kind: str = "text"                 # text / heading / table / notes
    extra: dict = field(default_factory=dict)


@dataclass
class ParsedDocument:
    blocks: list[ParsedBlock] = field(default_factory=list)
    meta: dict = field(default_factory=dict)   # 标题、作者、页数等，存进 chunks.extra 或 documents 供检索过滤

    @property
    def plain_text(self) -> str:
        """全部正文拼成一整篇（做纯文本预览或全文检索时用）"""
        return clean_text("\n\n".join(block.text for block in self.blocks if block.text))

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    def is_empty(self) -> bool:
        return not self.plain_text.strip()


def clean_text(text: str | None) -> str:
    """归一化空白：压缩行内多余空格、去掉行首尾空白、最多留一个空行

    解析器输出的原始文本里大量 \x07（docx 单元格标记）、连续空格、软回车，
    不归一化会让切块长度估算和检索命中都很脏。
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\x07", " ").replace("\u00a0", " ")
    lines = [_MULTI_BLANK.sub(" ", line).strip() for line in text.split("\n")]
    return _MULTI_NEWLINE.sub("\n\n", "\n".join(lines)).strip()


def merge_blocks(texts: Iterable[str], *, page: int | None = None, heading: str | None = None) -> list[ParsedBlock]:
    """把一批字符串转成 ParsedBlock，丢掉空白项"""
    return [
        ParsedBlock(text=cleaned, page=page, heading=heading)
        for cleaned in (clean_text(item) for item in texts)
        if cleaned
    ]


# 解析器签名：接收文件路径（避免把整个文件读进内存后还要传 bytes）
Parser = Callable[[Path], ParsedDocument]
