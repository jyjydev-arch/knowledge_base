#!/usr/bin/env python
"""建表脚本 —— 表结构以 app/db/models.py 为唯一真源

用法（在项目根目录执行）:
    app/.venv/bin/python scripts/init_db.py                 # 只建缺失的表（不动已有数据）
    app/.venv/bin/python scripts/init_db.py --drop           # 开发期重置：删光所有表再建（会清空数据！）
    app/.venv/bin/python scripts/init_db.py --check          # 库里表是否和模型对得上
    app/.venv/bin/python scripts/init_db.py --snapshot > init_db.sql   # 刷新那份可读快照

注意：create_all 只会「建缺失的表」，**不会给已有表加列/改类型**。
      改了 models.py 之后，开发库 --drop 重建，正式环境等接 alembic（TODO.md P1）。
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 app 包可被导入

from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db import models  # noqa: F401,E402  # 触发模型注册
from app.db.base import Base  # noqa: E402
from app.db.psql_config import engine  # noqa: E402

SNAPSHOT_HEADER = """-- ============================================================
-- AI 知识库 · PostgreSQL 建表脚本（快照）
--
-- 真源是 app/db/models.py，本文件由下面命令生成，请勿手工编辑：
--     app/.venv/bin/python scripts/init_db.py --snapshot > init_db.sql
-- 日常建表建议直接跑：
--     app/.venv/bin/python scripts/init_db.py            # 只建缺失的表
--     app/.venv/bin/python scripts/init_db.py --drop     # 开发期重置（清空数据）
--
-- 注意 1：本脚本只 CREATE ... IF NOT EXISTS，不会给已存在的表加列/改类型。
--         改过 models.py 后，开发库请 --drop 重建，正式环境等接 alembic（TODO.md P1）。
-- 注意 2：向量部分还没启用（pgvector 暂不处理）：chunks 表没有 embedding 列；
--         等向量方案定了再补 CREATE EXTENSION vector / vector(N) 列 / HNSW 索引（TODO.md P0-M3）。
--         本机 PG 17 目前没装 pgvector 扩展，直接跑 CREATE EXTENSION 会报错，所以先注释掉。
-- ============================================================

-- CREATE EXTENSION IF NOT EXISTS vector;   -- M3 启用向量化时再打开

-- 需要彻底重置时（会清空全部数据）手工执行下面几行：
-- DROP TABLE IF EXISTS chunks, documents, knowledge_bases, tokens, users CASCADE;

BEGIN;

"""


def render_sql() -> str:
    """按 PostgreSQL 方言渲染建表 DDL"""
    dialect = postgresql.dialect()
    parts = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in table.indexes:
            parts.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
    return "\n\n".join(parts)


def render_snapshot() -> str:
    """生成可重复执行的 init_db.sql：补上 IF NOT EXISTS"""
    ddl = render_sql()
    ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
    ddl = ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
    ddl = ddl.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
    return SNAPSHOT_HEADER + ddl + "\n\nCOMMIT;\n"


async def create(drop: bool) -> None:
    if drop:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        print("已删除所有表")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print(f"建表完成，共 {len(Base.metadata.tables)} 张表")


async def check() -> None:
    async with engine.connect() as conn:
        existing = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    for table in Base.metadata.sorted_tables:
        mark = "✅" if table.name in existing else "❌ 缺表"
        print(f"{mark}  {table.name}（{len(table.columns)} 列 / {len(table.indexes)} 索引）")
    extra = sorted(existing - set(Base.metadata.tables))
    if extra:
        print(f"⚠️  库里存在、模型里没有的表（可能是改名后遗留）: {extra}")


def main() -> int:
    parser = argparse.ArgumentParser(description="按 app/db/models.py 建表")
    parser.add_argument("--drop", action="store_true", help="先删光所有表再建（会清空数据）")
    parser.add_argument("--check", action="store_true", help="检查库里表是否齐全")
    parser.add_argument("--snapshot", action="store_true", help="输出 PostgreSQL 建表 SQL（刷 init_db.sql）")
    args = parser.parse_args()

    if args.snapshot:
        sys.stdout.write(render_snapshot())
        return 0

    masked = settings.DATABASE_URL
    if "@" in masked:
        head, tail = masked.split("@", 1)
        masked = head.split("://", 1)[0] + "://***@" + tail
    print(f"目标数据库: {masked}")

    if args.check:
        asyncio.run(check())
        return 0
    asyncio.run(create(drop=args.drop))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
