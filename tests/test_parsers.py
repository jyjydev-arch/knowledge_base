"""四种解析器 + 注册表的测试

docx / pptx 用 python-docx、python-pptx 现场造真文件（不提交二进制样本进仓库），
pdf 用一个手写的合法最小 PDF（pypdf 能读，且偏移量是算出来的，不是猜的）。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.api.v1 import documentrouter
from tests.test_document_api import assert_envelope
from app.services.parsers.base import DocumentParseError, UnsupportedFormatError, clean_text
from app.services.parsers.registry import MIME_BY_EXT, parse_file, supported_extensions


def minimal_pdf(text: str) -> bytes:
    """拼一个单页、含一行文字的合法 PDF（xref 偏移量真实计算）"""
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append((number, len(out)))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

    xref_start = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for _, offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref_start)
    return bytes(out)


def make_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("产品需求说明书", level=1)
    doc.add_paragraph("这是第一段正文，说明系统目标。")
    doc.add_heading("权限", level=2)
    doc.add_paragraph("共享库支持只读和读写两种权限。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "角色"
    table.cell(0, 1).text = "能做什么"
    table.cell(1, 0).text = "read"
    table.cell(1, 1).text = "看列表、详情、下载"
    doc.save(path)


def make_pptx(path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    first = prs.slides.add_slide(prs.slide_layouts[1])       # 标题 + 内容
    first.shapes.title.text = "季度复盘"
    first.placeholders[1].text = "营收增长 20%\n成本下降 5%"
    first.notes_slide.notes_text_frame.text = "备注：这一页要强调成本口径变化"

    second = prs.slides.add_slide(prs.slide_layouts[6])      # 空白页 + 表格
    shape = second.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
    shape.cell(0, 0).text = "指标"
    shape.cell(0, 1).text = "数值"
    shape.cell(1, 0).text = "命中率"
    shape.cell(1, 1).text = "0.83"
    prs.save(path)


# ---------------- Markdown / 纯文本 ----------------
def test_markdown_blocks_follow_heading_path(tmp_path):
    path = tmp_path / "spec.md"
    path.write_text(
        "# 一级\n正文A\n\n## 二级\n正文B\n\n### 三级\n正文C\n\n## 另一个二级\n正文D\n",
        encoding="utf-8",
    )
    doc = parse_file(path)
    headings = [b.text for b in doc.blocks if b.kind == "heading"]
    assert headings == ["一级", "二级", "三级", "另一个二级"]

    bodies = {b.text: b.heading for b in doc.blocks if b.kind == "text"}
    assert bodies["正文A"] == "一级"
    assert bodies["正文B"] == "一级 / 二级"
    assert bodies["正文C"] == "一级 / 二级 / 三级"
    assert bodies["正文D"] == "一级 / 另一个二级"        # 标题栈要正确回退


def test_markdown_front_matter_and_code_fence(tmp_path):
    path = tmp_path / "post.md"
    path.write_text('---\ntitle: 部署手册\nauthor: 张三\n---\n\n# 安装\n\n```bash\n# 不是标题\nnpm i\n```\n\n结尾正文\n', encoding="utf-8")
    doc = parse_file(path)
    assert doc.meta["title"] == "部署手册"
    assert doc.meta["author"] == "张三"
    assert all("不是标题" in b.text for b in doc.blocks if "不是标题" in b.text)   # 代码块里的 # 不能当标题
    assert "```bash" in doc.plain_text                                          # 代码块原样保留


def test_markdown_setext_heading(tmp_path):
    path = tmp_path / "setext.md"
    path.write_text("章节标题\n===\n\n内容在此\n", encoding="utf-8")
    doc = parse_file(path)
    assert doc.blocks[0].kind == "heading" and doc.blocks[0].text == "章节标题"


def test_markdown_blank_and_non_utf8(tmp_path):
    blank = tmp_path / "blank.md"
    blank.write_text("   \n\n  \n", encoding="utf-8")
    with pytest.raises(DocumentParseError):
        parse_file(blank)

    binary = tmp_path / "bad.md"
    binary.write_bytes("中文".encode("gbk"))
    with pytest.raises(DocumentParseError):
        parse_file(binary)


def test_txt_supported(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("第一段\n\n第二段\n", encoding="utf-8")
    doc = parse_file(path)
    assert "第一段" in doc.plain_text and "第二段" in doc.plain_text


def test_clean_text_normalizes_weird_whitespace():
    assert clean_text("a\u00a0\u00a0b") == "a b"          # no-break space
    assert clean_text("行一\x07\t  行二") == "行一 行二"   # docx 单元格标记
    assert clean_text("a\n\n\n\n\nb") == "a\n\nb"         # 最多留一个空行


# ---------------- docx ----------------
def test_docx_headings_and_table(tmp_path):
    path = tmp_path / "spec.docx"
    make_docx(path)
    doc = parse_file(path)

    assert [b.text for b in doc.blocks if b.kind == "heading"] == ["产品需求说明书", "权限"]
    body = {b.text: b.heading for b in doc.blocks if b.kind == "text"}
    assert body["这是第一段正文，说明系统目标。"] == "产品需求说明书"
    assert body["共享库支持只读和读写两种权限。"] == "产品需求说明书 / 权限"

    tables = [b for b in doc.blocks if b.kind == "table"]
    assert len(tables) == 1
    assert "角色 | 能做什么" in tables[0].text
    assert "read | 看列表、详情、下载" in tables[0].text
    assert tables[0].heading == "产品需求说明书 / 权限"   # 表格也挂在其所属标题下
    assert doc.meta["format"] == "docx"


def test_docx_rejects_fake_or_empty(tmp_path):
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"THIS IS NOT A DOCX FILE")
    with pytest.raises(DocumentParseError):
        parse_file(broken)

    from docx import Document

    empty = tmp_path / "empty.docx"
    Document().save(empty)
    with pytest.raises(DocumentParseError):
        parse_file(empty)


# ---------------- pptx ----------------
def test_pptx_pages_title_notes_and_table(tmp_path):
    path = tmp_path / "review.pptx"
    make_pptx(path)
    doc = parse_file(path)

    first = [b for b in doc.blocks if b.page == 1]
    assert any(b.kind == "notes" and "成本口径" in b.text for b in first)
    assert any(b.kind == "text" and "营收增长 20%" in b.text for b in first)
    assert all(b.heading == "季度复盘" for b in first if b.heading)      # 标题当页 heading，不混进正文

    tables = [b for b in doc.blocks if b.kind == "table"]
    assert tables and tables[0].page == 2
    assert "命中率 | 0.83" in tables[0].text
    assert doc.meta == {"format": "pptx", "slide_count": 2}


# ---------------- pdf ----------------
def test_pdf_pages(tmp_path):
    """合成 PDF 只能塞 ASCII：Helvetica + WinAnsi 表达不了中文，
    真实 PDF 的中文提取依赖文件内嵌字体，不能拿这个样本当中文能力的结论。"""
    path = tmp_path / "one.pdf"
    path.write_bytes(minimal_pdf("Knowledge Base Spec 2026"))
    doc = parse_file(path)
    assert doc.meta["format"] == "pdf" and doc.meta["page_count"] == 1
    assert doc.blocks[0].page == 1
    assert "Knowledge Base Spec 2026" in doc.blocks[0].text


def test_pdf_broken_and_no_text(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 then garbage")
    with pytest.raises(DocumentParseError):
        parse_file(broken)

    blank = tmp_path / "blank.pdf"
    blank.write_bytes(minimal_pdf(""))            # 能打开但没字 → 该报「提取不到文字」
    with pytest.raises(DocumentParseError, match="没有从 PDF 中提取到文字"):
        parse_file(blank)


# ---------------- 注册表 / 上传白名单一致性 ----------------
def test_registry_rejects_unknown_and_missing(tmp_path):
    with pytest.raises(UnsupportedFormatError):
        parse_file(tmp_path / "a.exe")

    with pytest.raises(DocumentParseError, match="不存在"):
        parse_file(tmp_path / "not-here.md")

    assert set(supported_extensions()) == {"pdf", "docx", "pptx", "md", "txt"}


def test_upload_whitelist_matches_registry():
    """白名单和解析器必须同源，否则会出现「能传不能解析」"""
    assert set(documentrouter.ALLOWED_EXTENSIONS) == set(supported_extensions())
    assert documentrouter.ALLOWED_EXTENSIONS["pptx"] == MIME_BY_EXT["pptx"]
    assert documentrouter.ALLOWED_EXTENSIONS["md"] == MIME_BY_EXT["md"]


def test_parse_document_script(tmp_path):
    """调试脚本要真能跑（M2 排错全靠它）"""
    path = tmp_path / "note.md"
    path.write_text("# 标题\n正文内容\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "scripts/parse_document.py", str(path), "--json"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["blocks"][0]["text"] == "标题"
    assert payload["blocks"][1]["text"] == "正文内容"


# ---------------- 与上传接口的联动 ----------------
def test_upload_accepts_pptx_because_registry_drives_whitelist(client, logged_in, run, monkeypatch, tmp_path):
    """新增一种格式后，上传接口要自动放行（白名单和解析器同源才有这个效果）"""
    from tests.test_document_api import make_kb, uploaded_files

    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"], name="幻灯片库")
    pptx_path = tmp_path / "review_src.pptx"
    make_pptx(pptx_path)

    with open(pptx_path, "rb") as fh:
        resp = client.post(
            "/api/v1/documents/upload",
            headers=logged_in["headers"],
            files={"file": ("review.pptx", fh.read(), "application/octet-stream")},
            params={"kb_id": kb_id},
        )
    body = assert_envelope(resp, 200)["data"]
    assert body["mime_type"] == MIME_BY_EXT["pptx"]
    assert body["status"] == "pending"        # 解析在 M2，先只登记
    assert uploaded_files(tmp_path, logged_in["user_id"], kb_id) == [f"{body['id']}.pptx"]
