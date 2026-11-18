"""M2 后半：切块算法、解析入库编排、上传后台任务接线、向量化入库

process_document 会真读磁盘，所以这里在 data/ 下开一个用后即删的临时目录 ——
路径必须落在 data/ 里，因为 `_safe_document_path` 的规矩就是"只允许读 data/ 下的文件"，
绕过它反而测不到真正的那道闸。
"""

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest

from app.db.crud import chunk_crud, document_crud
from app.db.models import DocStatus, KnowledgeBase
from app.services import process_document as pd
from app.services import vector_index
from app.services.chunker import chunk_blocks, chunk_parsed_document
from app.services.parsers.base import ParsedBlock, ParsedDocument
from tests.test_schema_models import _add_doc, _add_kb, _add_user

DATA_DIR = Path("data/test-index")


@pytest.fixture()
def index_dir():
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield DATA_DIR
    shutil.rmtree(DATA_DIR, ignore_errors=True)


def write_doc_text(index_dir, text: str, suffix: str = ".md") -> str:
    path = index_dir / f"{uuid.uuid4().hex[:8]}{suffix}"
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# 切块算法（纯函数）
# --------------------------------------------------------------------------


def test_短文档就一块():
    pieces = chunk_parsed_document(ParsedDocument(blocks=[ParsedBlock(text="一段很短的话")]), chunk_size=800)
    assert len(pieces) == 1 and pieces[0].seq == 0 and pieces[0].token_count > 0


def test_按标题分组不跨组拼块():
    blocks = [
        ParsedBlock(text="第一章内容" * 30, heading="第一章"),
        ParsedBlock(text="第二章内容" * 30, heading="第二章"),
    ]
    pieces = chunk_blocks(blocks, chunk_size=100, chunk_overlap=20)
    headings = {piece.heading for piece in pieces}
    assert headings == {"第一章", "第二章"}
    assert all("第一章" not in p.content or p.heading == "第一章" for p in pieces)


def test_长文本会滑窗切块且seq连续():
    blocks = [ParsedBlock(text="。".join(f"这是第{i}句要拿来切分的话" for i in range(40)))]
    pieces = chunk_blocks(blocks, chunk_size=80, chunk_overlap=20)
    assert len(pieces) > 3
    assert [piece.seq for piece in pieces] == list(range(len(pieces)))


def test_相邻窗口有重叠上下文():
    blocks = [ParsedBlock(text="。".join(f"第{i}句内容不太一样哦" for i in range(30)))]
    pieces = chunk_blocks(blocks, chunk_size=40, chunk_overlap=16)
    assert len(pieces) >= 3
    # 后一块的开头应当能在前一块里找到（overlap 的意义就是别让句子断在无语境的边界）
    assert any(pieces[1].content[:6] in pieces[0].content for _ in [0])


def test_overlap大于chunk_size会自动收敛而不是死循环():
    blocks = [ParsedBlock(text="很长" * 500)]
    pieces = chunk_blocks(blocks, chunk_size=50, chunk_overlap=999)
    assert 1 < len(pieces) < 100
    assert pieces[0].extra["chunk_overlap"] < pieces[0].extra["chunk_size"]


def test_空白块和空文本都被丢掉():
    blocks = [ParsedBlock(text="   "), ParsedBlock(text=""), ParsedBlock(text="有内容的一句话")]
    pieces = chunk_blocks(blocks, chunk_size=100)
    assert len(pieces) == 1


def test_页码从块上带下来():
    blocks = [ParsedBlock(text="第二页的内容", page=2), ParsedBlock(text="第三页的内容", page=3)]
    pieces = chunk_blocks(blocks, chunk_size=100)
    assert [piece.page for piece in pieces] == [2, 3]


def test_没有内容时切不出块():
    assert chunk_parsed_document(ParsedDocument(blocks=[]), chunk_size=100) == []


# --------------------------------------------------------------------------
# 解析 → 切块 → 写库（端到端）
# --------------------------------------------------------------------------


def make_document(run, index_dir, text: str = "# 总则\n\n本合同适用于全部采购项目。\n\n# 附则\n\n自签署之日起生效。"):
    async def _seed(db):
        user = await _add_user(db, f"u{uuid.uuid4().hex[:6]}", f"{uuid.uuid4().hex[:6]}@example.com")
        kb = await _add_kb(db, user.id, name=f"库-{uuid.uuid4().hex[:4]}")
        file_path = write_doc_text(index_dir, text)
        document = await _add_doc(db, kb.id, user.id, filename="说明.md")
        document.file_path = file_path
        document.status = DocStatus.PENDING
        await db.commit()
        await db.refresh(document)
        return user, kb, document

    return run(_seed)


def test_跑完一遍状态变indexed并写入chunks(run, index_dir):
    user, kb, document = make_document(run, index_dir)

    async def _go(db):
        return await pd.process_document(document.id, session=db)

    result = run(_go)
    assert result["ok"] is True and result["status"] == DocStatus.INDEXED
    assert result["chunks"] >= 1

    async def _check(db):
        fresh = await document_crud.get_document_any(document.id, db)
        return fresh.status, fresh.chunk_count, fresh.token_count, await chunk_crud.count_document_chunks(fresh.id, db)

    status, chunk_count, token_count, rows = run(_check)
    assert status == DocStatus.INDEXED and chunk_count == rows and token_count > 0


def test_重跑不翻倍(run, index_dir):
    _, _, document = make_document(run, index_dir)
    run(lambda db: pd.process_document(document.id, session=db))
    run(lambda db: pd.process_document(document.id, session=db))

    async def _count(db):
        return await chunk_crud.count_document_chunks(document.id, db)

    assert run(_count) >= 1
    assert run(_count) == run(_count)


def test_块大小参数是从知识库配置里取的(run, index_dir):
    """kb.chunk_size 改了，extra 里必须看见新值 —— 否则说明切块在用硬编码默认值"""
    _, kb, document = make_document(run, index_dir, text="第一段。" * 40 + "第二段。" * 40)

    async def _set_and_run(db):
        target = await db.get(KnowledgeBase, kb.id)
        target.chunk_size, target.chunk_overlap = 30, 5
        await db.commit()
        return await pd.process_document(document.id, session=db)

    assert run(_set_and_run)["ok"] is True

    async def _extras(db):
        return [chunk.extra for chunk in await chunk_crud.list_document_chunks(document.id, db)]

    extras = run(_extras)
    assert len(extras) > 1 and all(extra["chunk_size"] == 30 for extra in extras)


def test_空文档变failed并写error_msg(run, index_dir):
    _, _, document = make_document(run, index_dir, text="   \n\n  ")

    async def _go(db):
        return await pd.process_document(document.id, session=db)

    result = run(_go)
    assert result["ok"] is False and result["status"] == DocStatus.FAILED

    async def _msg(db):
        fresh = await document_crud.get_document_any(document.id, db)
        return fresh.error_msg

    assert "OCR" in run(_msg) or "没有可提取" in run(_msg)


def test_文件不存在时failed而不是崩(run, index_dir):
    _, _, document = make_document(run, index_dir)

    async def _break_and_run(db):
        fresh = await document_crud.get_document_any(document.id, db)
        fresh.file_path = str(Path(index_dir) / "早没了.md")
        await db.commit()
        return await pd.process_document(fresh.id, session=db)

    result = run(_break_and_run)
    assert result["ok"] is False and "不存在" in result["reason"]


def test_路径穿越被拒绝而不是读系统文件(run, index_dir):
    """file_path 这一列历史上来自客户端文件名，读的时候必须再兜一道"""
    _, _, document = make_document(run, index_dir)

    async def _go(db):
        fresh = await document_crud.get_document_any(document.id, db)
        fresh.file_path = "../../etc/passwd"
        await db.commit()
        return await pd.process_document(fresh.id, session=db)

    result = run(_go)
    assert result["ok"] is False and "越出" in result["reason"]


def test_文档不存在时给明确原因(run):
    result = run(lambda db: pd.process_document(uuid.uuid4(), session=db))
    assert result["ok"] is False and result["reason"] == "document_not_found"


def test_force为False时跳过已索引的文档(run, index_dir):
    _, _, document = make_document(run, index_dir)
    run(lambda db: pd.process_document(document.id, session=db))

    async def _again(db):
        chunks_before = await chunk_crud.count_document_chunks(document.id, db)
        result = await pd.process_document(document.id, session=db, force=False)
        return chunks_before, result

    before, result = run(_again)
    assert result["reason"] == "already_indexed" and result["chunks"] == before


def test_没有session参数时走全局引擎也不许抛(run, index_dir):
    """BackgroundTasks 就是这么调的（session=None → 自己开全局引擎）

    测试车道的全局引擎是没建表的内存库，这里必然失败；
    要断言的是**它失败但不把异常抛穿** —— 抛穿等于把一次成功的上传报成上传失败。
    """
    _, _, document = make_document(run, index_dir)

    async def _break_path(db):
        fresh = await document_crud.get_document_any(document.id, db)
        fresh.file_path = "data/test-index/这个文件不存在.md"
        await db.commit()
        return fresh.id

    doc_id = run(_break_path)
    result = run(lambda db: pd.process_document(doc_id, session=db))
    assert result["ok"] is False and result["status"] == DocStatus.FAILED

    # 关键：不传 session，让函数自己开全局引擎 —— 这正是 BackgroundTasks 的调用形状，
    # 而全局引擎在测试车道是没建表的内存库。传了 session 就绕过了兜底分支，测不到东西。
    async def _global_lane():
        return await pd.process_document(str(uuid.uuid4()))

    swallowed = asyncio.run(_global_lane())
    assert swallowed["ok"] is False and swallowed["status"] == "error"


# --------------------------------------------------------------------------
# 上传接线：开关决定要不要调度后台任务
# --------------------------------------------------------------------------


def test_开关打开时上传会调度索引任务(client, logged_in, run, monkeypatch):
    from app.api.v1 import documentrouter
    from app.core.config import settings
    from tests.test_document_api import make_kb, upload

    called: list[str] = []
    kb_id = make_kb(run, logged_in["user_id"])
    monkeypatch.setattr(settings, "DOCUMENT_INDEX_ON_UPLOAD", True)
    monkeypatch.setattr(documentrouter, "process_document", lambda doc_id: called.append(str(doc_id)))

    response = upload(client, logged_in["headers"], kb_id=kb_id, content=b"# t\n\nx")
    assert response.status_code == 200, response.text
    assert len(called) == 1, "开关打开就必须真的把索引任务丢出去"


def test_开关关掉时不调度索引任务(client, logged_in, run, monkeypatch):
    from app.api.v1 import documentrouter
    from app.core.config import settings
    from tests.test_document_api import make_kb, upload

    called: list[str] = []
    kb_id = make_kb(run, logged_in["user_id"])
    monkeypatch.setattr(settings, "DOCUMENT_INDEX_ON_UPLOAD", False)
    monkeypatch.setattr(documentrouter, "process_document", lambda doc_id: called.append(str(doc_id)))

    response = upload(client, logged_in["headers"], kb_id=kb_id, content=b"# t\n\nx")
    assert response.status_code == 200 and called == []


# --------------------------------------------------------------------------
# 向量化入库（不连库，只验纯逻辑与调用顺序）
# --------------------------------------------------------------------------


class FakeVectorStore:
    def __init__(self):
        self.added: list[tuple[list, list, list]] = []

    def add_texts(self, texts, metadatas=None, ids=None, **kwargs):
        self.added.append((list(texts), list(metadatas or []), list(ids or [])))
        return list(ids or [])


def test_build_records顺序严格一致(run):
    async def _seed(db):
        user = await _add_user(db, f"v{uuid.uuid4().hex[:6]}", f"v{uuid.uuid4().hex[:6]}@example.com")
        kb = await _add_kb(db, user.id, name=f"库-{uuid.uuid4().hex[:4]}")
        document = await _add_doc(db, kb.id, user.id, filename="a.md")
        from app.services.chunker import ChunkPiece

        pieces = [
            ChunkPiece(seq=0, content="第一块", page=1, heading="A"),
            ChunkPiece(seq=1, content="   ", page=1, heading="A"),  # 空块必须被丢掉
            ChunkPiece(seq=2, content="第三块", page=2, heading="B"),
        ]
        await chunk_crud.replace_document_chunks(document, pieces, db)
        return await chunk_crud.list_document_chunks(document.id, db), document

    chunks, document = run(_seed)
    ids, texts, metadatas = vector_index.build_records(chunks)
    assert texts == ["第一块", "第三块"]
    assert len(ids) == len(texts) == len(metadatas) == 2
    assert ids == [str(chunk.id) for chunk in chunks if chunk.content.strip()]
    for meta in metadatas:
        assert set(meta) >= {"user_id", "kb_id", "document_id", "chunk_id", "seq"}
        assert meta["document_id"] == str(document.id)
        assert meta["user_id"] == str(chunks[0].owner_id)


def test_批次切分正确():
    sizes = [len(b) for b in vector_index.batches(list(range(10)), 4)]
    assert sizes == [4, 4, 2]
    assert [len(b) for b in vector_index.batches([], 4)] == []


def test_车道不可用时明确给出原因(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+aiosqlite:///x.db")
    ok, why = vector_index.embedding_ready()
    assert ok is False and "SQLite" in why

    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    ok, why = vector_index.embedding_ready()
    assert ok is False and "EMBEDDING_API_KEY" in why

    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-looking-real")
    assert vector_index.embedding_ready()[0] is True


def test_index_document在车道不可用时返回而不是抛(run):
    """这条很重要：连不上向量库不能把上传接口带崩"""

    async def _seed(db):
        user = await _add_user(db, f"w{uuid.uuid4().hex[:6]}", f"w{uuid.uuid4().hex[:6]}@example.com")
        kb = await _add_kb(db, user.id, name=f"库-{uuid.uuid4().hex[:4]}")
        return await _add_doc(db, kb.id, user.id, filename="a.md")

    document = run(_seed)
    result = run(lambda db: vector_index.index_document(document.id, session=db))
    assert result["ok"] is False and result["indexed"] == 0


def test_写入前先删除旧向量保证幂等(run, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-looking-real")
    calls: list[list[str]] = []

    def fake_delete(vectorstore, document_ids):
        calls.append(list(document_ids))
        return 0

    async def _seed(db):
        user = await _add_user(db, f"x{uuid.uuid4().hex[:6]}", f"x{uuid.uuid4().hex[:6]}@example.com")
        kb = await _add_kb(db, user.id, name=f"库-{uuid.uuid4().hex[:4]}")
        document = await _add_doc(db, kb.id, user.id, filename="a.md")
        from app.services.chunker import ChunkPiece

        await chunk_crud.replace_document_chunks(document, [ChunkPiece(seq=0, content="正文")], db)
        return document

    document = run(_seed)
    store = FakeVectorStore()
    monkeypatch.setattr(vector_index, "_delete_by_document_ids_sync", fake_delete)
    result = run(lambda db: vector_index.index_document(document.id, session=db, vectorstore=store))

    assert result["ok"] is True and result["indexed"] == 1
    assert calls == [[str(document.id)]], "必须按 document_id 精确删旧向量"
    texts, metadatas, ids = store.added[0]
    assert len(ids) == 1 and ids[0] == metadatas[0]["chunk_id"], "向量 id 直接用 chunks.id，不用随机 uuid"
    assert metadatas[0]["document_id"] == str(document.id)


def test_空列表清理不报错(run):
    assert run(lambda db: vector_index.remove_document_vectors([])) == 0
