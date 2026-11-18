"""文档落盘 / 路径约定 / 删除 / 解析入库编排

路径约定是本项目的硬规矩，唯一来源是 document_storage_path()：
    库主人上传      → data/{kb.owner_id}/{kb_id}/{document_id}.{ext}
    被授权人上传    → data/share/{kb_id}/{document_id}.{ext}
落盘、删除、列目录、边界校验、解析入库都必须复算同一条规则，复算不一致就只删记录不碰磁盘。

process_document() 是 M2 的编排入口：解析 → 切块 → 写 chunks → 推进 status。
它放在 service 层，不碰 HTTP：失败只返回结构化结果 + 把 documents.status 写成 failed，
由调用方（BackgroundTasks / 未来的 worker）决定要不要提示用户。
"""

import asyncio
import logging
import os
import re
import shutil
import uuid as uuid_lib
from pathlib import Path as FsPath

from fastapi import UploadFile

logger = logging.getLogger("app.services.document")

DATA_ROOT = "data"
SHARE_ROOT = "data/share"  # 别人共享给我的库里，我上传的文件放这里

# 拼路径前必须先过这两条：uuid 段 + 纯字母数字扩展名
# （就算 user_id/kb_id 来自数据库或 token，也不能赌它一定干净 —— 这是唯一一道防目录穿越的闸）
SAFE_UUID_SEGMENT = re.compile(r"^[0-9a-fA-F]{1}(?:-?[0-9a-fA-F]){7,63}$")
SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _segment(value, name: str) -> str:
    """校验并**规范化**成带连字符的 uuid 字符串

    规范化这步不是洁癖：同一个 uuid 允许写成有连字符/无连字符/大小写混三种样子，
    不统一的话同一个库会散成好几个目录，对账永远对不上。
    """
    text = str(value)
    compact = text.replace("-", "")
    if not SAFE_UUID_SEGMENT.match(compact):
        raise ValueError(f"{name} 不是合法 uuid，拒绝拼路径: {text!r}")
    return str(uuid_lib.UUID(compact))


def _extension(ext: str) -> str:
    text = str(ext).lower()
    if not SAFE_EXT.match(text):
        raise ValueError(f"扩展名不合法，拒绝拼路径: {ext!r}")
    return text


def kb_own_dir(user_id, kb_id) -> str:
    """库主人自己上传的文件目录：data/{user_id}/{kb_id}"""
    return f"{DATA_ROOT}/{_segment(user_id, 'user_id')}/{_segment(kb_id, 'kb_id')}"


def kb_share_dir(kb_id) -> str:
    """被授权人上传的文件目录：data/share/{kb_id}"""
    return f"{SHARE_ROOT}/{_segment(kb_id, 'kb_id')}"


def document_storage_path(kb_owner_id, uploader_id, kb_id, document_id, ext: str) -> str:
    """唯一的存储路径规则（两个分支都由参数完全可复算，所以事后能算出该删哪份文件）

        库主人自己传： data/{kb_owner_id}/{kb_id}/{document_id}.{ext}
        被授权的人传： data/share/{kb_id}/{document_id}.{ext}

    为什么这么分：
      - 被授权人的文件绝不写进库主人的个人目录（否则 B 的磁盘配额被 A 吃掉，
        删 A 账号时那些文件还会留在 B 目录里变成孤儿）；
      - 也不放 data/{uploader}/ —— 那会让一个库的文件散到 N 个用户目录里，
        对账/备份/删库要遍历一堆地方；
      - 所以上传者目录只装「自己的库」，共享协作一律进 data/share/，
        一个库最多两个目录，路径由 (库owner, 上传者) 决定，可复算。
    """
    if str(kb_owner_id) == str(uploader_id):
        return f"{kb_own_dir(kb_owner_id, kb_id)}/{_segment(document_id, 'document_id')}.{_extension(ext)}"
    return f"{kb_share_dir(kb_id)}/{_segment(document_id, 'document_id')}.{_extension(ext)}"


def kb_storage_dirs(kb_owner_id, kb_id) -> list[str]:
    """某个知识库可能用到的全部目录（对账时两处都要看）"""
    return [kb_own_dir(kb_owner_id, kb_id), kb_share_dir(kb_id)]


def new_document_file_path(
    file: UploadFile, kb_owner_id: str, uploader_id: str, kb_id: str, document_id: str, ext: str
) -> str:
    """把上传的文件落盘，返回相对路径

    - 先建目录：每个库第一次上传时目录还不存在；
    - copyfileobj 流式拷贝，不把整个文件读进内存；
    - 写失败就删掉半截文件再抛 OSError，由调用方决定怎么回滚（service 层不碰 HTTP）。
    """
    file_path = document_storage_path(kb_owner_id, uploader_id, kb_id, document_id, ext)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    try:
        with open(file_path, "wb") as target:
            shutil.copyfileobj(file.file, target)
    except OSError:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise

    return file_path


def delete_document_file(
    kb_owner_id: str, uploader_id: str, kb_id: str, documents_id: str, documents_format: str
) -> bool:
    """删除磁盘文件：删掉返回 True，文件本来就不在返回 False（不抛异常）

    uploader_id 传 documents.owner_id（上传者），这样能精确复算出当初落盘的位置。
    """
    file_path = document_storage_path(kb_owner_id, uploader_id, kb_id, documents_id, documents_format)
    if os.path.exists(file_path):
        os.remove(file_path)
        return True
    return False


def search_document_files(kb_owner_id, kb_id) -> list[str]:
    """列出该库两个可能目录里的文件名（合并去重）

    目录不存在返回空列表而不是抛 FileNotFoundError —— 否则 /status 对新建空库直接 500。
    """
    names: set[str] = set()
    for directory in kb_storage_dirs(kb_owner_id, kb_id):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if os.path.isfile(os.path.join(directory, name)):
                names.add(name)
    return sorted(names)


def missing_file_paths(file_paths: list[str]) -> list[str]:
    """库里记录说文件应该存在，实际不在 —— 按每条记录的 file_path 精确判，最可靠"""
    return [path for path in file_paths if not os.path.isfile(path)]


def remove_kb_directories_if_empty(kb_owner_id, kb_id) -> bool:
    """把空出来的库目录收掉（两个目录都清；只剩空目录才删，绝不 rmtree）"""
    removed = False
    for directory in kb_storage_dirs(kb_owner_id, kb_id):
        try:
            if os.path.isdir(directory) and not os.listdir(directory):
                os.rmdir(directory)
                removed = True
        except OSError:
            continue
    return removed


# ======================= 解析 → 切块 → 入库（M2 编排） =======================


class DocumentProcessError(Exception):
    """编排里的可预期失败（文件没了、路径不合法、文档不存在）"""


def _safe_document_path(file_path: str) -> str:
    """把库里的 file_path 变成可打开的绝对路径，并确认它没有跑出 data/

    file_path 这一列最早来源于客户端提交的文件名，虽然现在写入前已经 sanitize 过，
    但读的时候再兜一道才划算：`../../etc/passwd` 存进这一列的话，解析任务会去读系统文件。
    """
    raw = str(file_path or "").strip()
    if not raw:
        raise DocumentProcessError("文档没有存储路径")
    resolved = FsPath(raw).resolve()
    root = FsPath(DATA_ROOT).resolve()
    if root not in resolved.parents and resolved != root:
        raise DocumentProcessError(f"文档路径越出 data/ 目录，拒绝读取: {raw}")
    if not resolved.is_file():
        raise DocumentProcessError(f"文件不存在: {raw}")
    return str(resolved)


async def process_document(document_id, *, session=None, force: bool = True) -> dict:
    """解析并入库一篇文档，返回 {"ok", "status", "chunks", "tokens", "reason"}

    幂等：切块走 chunk_crud.replace_document_chunks（先删后插），重跑不会翻倍。
    状态流转：pending/parsing → chunking → indexed，任一步失败 → failed + error_msg。
    force=False 时跳过已经 indexed 的文档（给"批量重建索引"脚本用，省一次全文解析）。

    session 传 None 就自己开一个 —— BackgroundTasks 里请求级 Session 已经关闭了，
    绝不能复用请求那个，否则任务里拿到的是已归还到池子的连接。

    异常边界（有意为之）：**只有自己开 Session 时才兜住异常**。
    调用方显式传 session 说明它在事务里，异常该往上抛让它自己决定回滚；
    后台任务那条路必须吞掉并返回结果，否则一次索引失败会被前端读成"上传失败"。
    """
    from app.db.crud import chunk_crud, document_crud, kb_crud
    from app.db.models import DocStatus
    from app.rag.memory.base import MemoryError  # noqa: F401  预留：向量化阶段会复用记忆层的错误类型
    from app.services.chunker import chunk_parsed_document
    from app.services.parsers import registry
    from app.services.parsers.base import DocumentParseError

    async def _run(db) -> dict:
        document = await document_crud.get_document_any(document_id, db)  # noqa: F841
        if document is None:
            return {"ok": False, "status": "missing", "chunks": 0, "tokens": 0, "reason": "document_not_found"}
        if document.status == DocStatus.INDEXED and not force:
            return {"ok": True, "status": document.status, "chunks": document.chunk_count,
                    "tokens": document.token_count, "reason": "already_indexed"}

        await document_crud.update_status(document, DocStatus.PARSING, db)
        try:
            path = _safe_document_path(document.file_path)
        except DocumentProcessError as exc:
            await document_crud.update_status(document, DocStatus.FAILED, db, error_msg=str(exc)[:500])
            return {"ok": False, "status": DocStatus.FAILED, "chunks": 0, "tokens": 0, "reason": str(exc)}

        try:
            parsed = await asyncio.to_thread(registry.parse_file, path)
        except DocumentParseError as exc:
            message = f"解析失败：{exc}"
            await document_crud.update_status(document, DocStatus.FAILED, db, error_msg=message[:500])
            return {"ok": False, "status": DocStatus.FAILED, "chunks": 0, "tokens": 0, "reason": message}
        except Exception as exc:  # noqa: BLE001 —— 三方库的意外异常也必须落到 failed，不能把任务崩掉
            logger.exception("解析文档异常 document_id=%s", document_id)
            message = f"解析异常：{type(exc).__name__}"
            await document_crud.update_status(document, DocStatus.FAILED, db, error_msg=message[:500])
            return {"ok": False, "status": DocStatus.FAILED, "chunks": 0, "tokens": 0, "reason": message}

        if parsed.is_empty():
            message = "没有可提取的正文（可能是扫描件，需要 OCR）"
            await document_crud.update_status(document, DocStatus.FAILED, db, error_msg=message)
            return {"ok": False, "status": DocStatus.FAILED, "chunks": 0, "tokens": 0, "reason": message}

        kb = await kb_crud.get_kb(document.kb_id, db)
        pieces = chunk_parsed_document(
            parsed,
            chunk_size=getattr(kb, "chunk_size", None),
            chunk_overlap=getattr(kb, "chunk_overlap", None),
        )

        await document_crud.update_status(document, DocStatus.CHUNKING, db)
        chunk_count, token_count = await chunk_crud.replace_document_chunks(document, pieces, db)
        await document_crud.set_chunk_stats(document, chunk_count, token_count, db)
        await document_crud.update_status(document, DocStatus.INDEXED, db, error_msg=None)
        logger.info("文档入库完成 document_id=%s chunks=%d tokens=%d", document_id, chunk_count, token_count)
        return {"ok": True, "status": DocStatus.INDEXED, "chunks": chunk_count, "tokens": token_count,
                "reason": None}

    if session is not None:
        return await _run(session)

    from app.db.psql_config import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            return await _run(db)
        except Exception as exc:  # noqa: BLE001
            # 索引是上传之后的事。这里抛出去，前端看到的就是一次"上传失败"，
            # 而实际上文件已经落盘、记录已经入库 —— 那才是真的误导。
            logger.exception("文档索引任务失败 document_id=%s", document_id)
            return {"ok": False, "status": "error", "chunks": 0, "tokens": 0,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
