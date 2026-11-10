#!/usr/bin/env python
"""解析预览脚本：把一个文件解析成结构化文本打出来，方便调解析器

用法（项目根）:
    .venv/bin/python scripts/parse_document.py 需求说明书.docx
    .venv/bin/python scripts/parse_document.py slides.pptx --json      # 给 M2 排错用
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.parsers.base import DocumentParseError  # noqa: E402
from app.services.parsers.registry import parse_file, supported_extensions  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="预览文档解析结果（不写数据库）")
    parser.add_argument("file", help="要解析的文件路径")
    parser.add_argument("--ext", default=None, help="强制指定扩展名（默认按文件后缀）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（blocks + meta）")
    parser.add_argument("--max-chars", type=int, default=200, help="每块预览长度")
    args = parser.parse_args()

    try:
        document = parse_file(args.file, ext=args.ext)
    except DocumentParseError as exc:
        print(f"❌ 解析失败: {exc}", file=sys.stderr)
        print(f"   目前支持: {'/'.join(supported_extensions())}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "meta": document.meta,
            "blocks": [
                {"text": block.text, "page": block.page, "heading": block.heading,
                 "kind": block.kind, "extra": block.extra}
                for block in document.blocks
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"文件: {args.file}")
    print(f"meta: {document.meta}")
    print(f"块数: {document.block_count}｜总字数: {len(document.plain_text)}")
    print("-" * 72)
    for index, block in enumerate(document.blocks, start=1):
        marker = f"[{block.kind}]"
        location = f"第{block.page}页" if block.page else "无页码"
        heading = f"《{block.heading}》" if block.heading else ""
        preview = block.text.replace("\n", " ⏎ ")[: args.max_chars]
        print(f"{index:>3} {marker:<9} {location:<7} {heading} {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
