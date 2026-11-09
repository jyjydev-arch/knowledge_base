"""文档接口测试：/documents 的上传、列表、删除"""

import pytest

from sqlalchemy import func
from sqlalchemy import select as sa_select

import app.api.v1.documentrouter as documentrouter
from app.db.crud import document_crud
from app.db.models import Chunk, Document, KnowledgeBase
from app.services.process_document import delete_document_file
from tests.test_schema_models import _add_doc, _add_kb, _u  # 复用建数据的辅助函数


def assert_envelope(resp, status: int):
    body = resp.json()
    assert resp.status_code == status, resp.text
    assert set(body.keys()) == {"status", "msg", "data"}, body
    assert body["status"] == status, body
    return body


def seed_documents(run, user_id, filenames=("手册.md", "需求.docx"), name="工作笔记"):
    """建一个知识库 + 若干文档，返回 (kb_id, [document_id...])"""

    async def _seed(session):
        kb = await _add_kb(session, user_id, name=name, description="产品资料")
        docs = [await _add_doc(session, kb.id, user_id, filename=f) for f in filenames]
        return str(kb.id), [str(d.id) for d in docs]

    return run(_seed)


def test_list_empty_is_ok(client, logged_in):
    """没有文档是正常情况：200 + items=[]，不是 404"""
    body = assert_envelope(client.get("/api/v1/documents/list", headers=logged_in["headers"]), 200)
    data = body["data"]
    assert data == {"items": [], "total": 0, "page": 1, "size": 20}


def test_list_requires_login(client):
    assert_envelope(client.get("/api/v1/documents/list"), 401)


def test_list_returns_serialized_documents(client, logged_in, run):
    kb_id, _ = seed_documents(run, logged_in["user_id"])
    body = assert_envelope(client.get("/api/v1/documents/list", headers=logged_in["headers"]), 200)
    data = body["data"]

    assert data["total"] == 2
    # created_at desc + title 兜底，同秒插入时按标题排序，结果稳定
    assert [item["title"] for item in data["items"]] == ["手册.md", "需求.docx"]

    item = data["items"][0]
    assert item["kb_id"] == kb_id
    assert item["status"] == "pending"
    assert item["file_size"] == 1024
    assert item["download_url"].startswith("/documents/download/")
    assert "file_path" not in item and "file_hash" not in item  # 不把内部路径/哈希透出去


def test_list_filter_by_kb(client, logged_in, run):
    kb_a, _ = seed_documents(run, logged_in["user_id"], filenames=("A.md",), name="库A")
    kb_b, _ = seed_documents(run, logged_in["user_id"], filenames=("B.md",), name="库B")

    body = assert_envelope(
        client.get(f"/api/v1/documents/list?kb_id={kb_b}", headers=logged_in["headers"]), 200
    )
    assert [i["title"] for i in body["data"]["items"]] == ["B.md"]

    bad = assert_envelope(
        client.get("/api/v1/documents/list?kb_id=not-a-uuid", headers=logged_in["headers"]), 200
    )
    assert bad["data"]["total"] == 0  # 非法 id 就当没有匹配，不报 500


def test_list_is_isolated_per_user(client, logged_in, other_headers, run):
    """越权隔离：lisi 的文档绝不会出现在 zhangsan 的列表里"""

    async def _seed_other(session):
        from app.db.models import User
        from sqlalchemy import select

        lisi = (await session.execute(select(User).where(User.username == "lisi"))).scalar_one()
        kb = await _add_kb(session, lisi.id, name="lisi的库")
        return await _add_doc(session, kb.id, lisi.id, filename="机密.md")

    other_doc = run(_seed_other)

    body = assert_envelope(client.get("/api/v1/documents/list", headers=logged_in["headers"]), 200)
    assert body["data"]["total"] == 0
    assert str(other_doc.id) not in str(body)


def test_pagination(client, logged_in, run):
    seed_documents(run, logged_in["user_id"], filenames=("a.md", "b.md", "c.md"))

    page1 = assert_envelope(
        client.get("/api/v1/documents/list?page=1&size=2", headers=logged_in["headers"]), 200
    )["data"]
    assert page1["total"] == 3 and len(page1["items"]) == 2

    page2 = assert_envelope(
        client.get("/api/v1/documents/list?page=2&size=2", headers=logged_in["headers"]), 200
    )["data"]
    assert len(page2["items"]) == 1

    assert_envelope(client.get("/api/v1/documents/list?page=0", headers=logged_in["headers"]), 422)
    assert_envelope(client.get("/api/v1/documents/list?size=999", headers=logged_in["headers"]), 422)


# ---------------- DELETE /documents/delete/{document_id} ----------------
def canonical_file_path(user_id: str, kb_id: str, document_id) -> str:
    """document_storage_path 的「库主人自己上传」分支：data/{owner}/{kb_id}/{doc_id}.md"""
    return f"data/{user_id}/{kb_id}/{document_id}.md"


# 语义更清楚的别名（上传者就是库主人时用它）
own_file_path = canonical_file_path


def share_file_path(kb_id, document_id) -> str:
    """document_storage_path 的「被授权人上传」分支：data/share/{kb_id}/{doc_id}.md"""
    return f"data/share/{kb_id}/{document_id}.md"


def seed_document_with_file(run, logged_in, monkeypatch, tmp_path, *, file_path=None, with_file=True, chunks=2):
    """建库 + 建文档 + 落磁盘文件 + 造 chunk，返回 (document_id, kb_id)"""
    uid = logged_in["user_id"]

    async def _seed(session):
        kb = await _add_kb(session, uid, name="待删库")
        doc = await _add_doc(session, kb.id, uid, filename="要删的文档.md", file_hash="del-hash")
        stored = file_path or canonical_file_path(uid, kb.id, doc.id)
        doc.file_path = stored
        await session.commit()
        for seq in range(chunks):
            session.add(
                Chunk(document_id=doc.id, kb_id=kb.id, owner_id=_u(uid), seq=seq, content=f"块{seq}")
            )
        doc.chunk_count = chunks          # 正常流程由 M2 的解析任务维护，这里手工对齐
        await session.commit()
        return str(doc.id), str(kb.id), stored

    monkeypatch.chdir(tmp_path)
    doc_id, kb_id, stored = run(_seed)
    if with_file:
        target = tmp_path / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("正文内容")
    return doc_id, kb_id


def test_delete_removes_record_chunks_and_file(client, logged_in, run, monkeypatch, tmp_path):
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path)
    on_disk = tmp_path / canonical_file_path(logged_in["user_id"], kb_id, doc_id)
    assert on_disk.exists()

    resp = client.delete(f"/api/v1/documents/delete/{doc_id}", headers=logged_in["headers"])
    body = assert_envelope(resp, 200)
    assert body["msg"] == "文档已删除"
    assert body["data"] is None

    # 文件、记录、chunk 都没了
    assert not on_disk.exists()

    async def _check(session):
        docs = await session.execute(sa_select(Document).where(Document.id == _u(doc_id)))
        chunks_left = await session.scalar(sa_select(func.count()).select_from(Chunk))
        kb = await session.get(KnowledgeBase, _u(kb_id))
        return docs.scalar(), chunks_left, kb.version, kb.document_count, kb.chunk_count

    doc, chunks_left, version, doc_count, chunk_count = run(_check)
    assert doc is None
    assert (chunks_left, version, doc_count, chunk_count) == (0, 2, 0, 0)


def test_delete_without_file_still_reports_ok(client, logged_in, run, monkeypatch, tmp_path):
    """磁盘文件本来就不在：业务上删除成功，msg 里说明清楚，不能报 500"""
    doc_id, _ = seed_document_with_file(
        run, logged_in, monkeypatch, tmp_path, with_file=False
    )
    body = assert_envelope(
        client.delete(f"/api/v1/documents/delete/{doc_id}", headers=logged_in["headers"]), 200
    )
    assert "不存在" in body["msg"]


def test_delete_rejects_path_traversal(client, logged_in, run, monkeypatch, tmp_path):
    """边界：file_path 被写成 data/<uid>/../secret.txt 时，只删记录，绝不删别人目录外的文件"""
    secret = tmp_path / "secret.txt"
    secret.write_text("别的东西")
    doc_id, _ = seed_document_with_file(
        run,
        logged_in,
        monkeypatch,
        tmp_path,
        file_path=f"data/{logged_in['user_id']}/../secret.txt",
        with_file=False,
    )

    body = assert_envelope(
        client.delete(f"/api/v1/documents/delete/{doc_id}", headers=logged_in["headers"]), 200
    )
    assert "路径不合法" in body["msg"]
    assert secret.exists() and secret.read_text() == "别的东西"


def test_delete_skips_file_outside_canonical_layout(client, logged_in, run, monkeypatch, tmp_path):
    """边界：库里存的是 data/<uid>/documents/xxx.md（和 delete_document_file 的拼法不一致）
    → 认定路径不可信，只删记录，避免删错文件"""
    stray = tmp_path / "data" / logged_in["user_id"] / "其它目录" / "x.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("内容")
    doc_id, _ = seed_document_with_file(
        run,
        logged_in,
        monkeypatch,
        tmp_path,
        file_path=f"data/{logged_in['user_id']}/其它目录/x.md",
        with_file=False,
    )

    body = assert_envelope(
        client.delete(f"/api/v1/documents/delete/{doc_id}", headers=logged_in["headers"]), 200
    )
    assert "路径不合法" in body["msg"]
    assert stray.exists()  # 没被误删


def test_delete_other_users_document_is_404(client, logged_in, other_headers, run, monkeypatch, tmp_path):
    """越权：别人的文档 id 一律 404，且对方的数据完好"""

    async def _seed_other(session):
        from sqlalchemy import select as _select

        from app.db.models import User

        lisi = (await session.execute(_select(User).where(User.username == "lisi"))).scalar_one()
        kb = await _add_kb(session, lisi.id, name="lisi的库")
        doc = await _add_doc(session, kb.id, lisi.id, filename="机密.md", file_hash="lisi-hash")
        return str(doc.id)

    monkeypatch.chdir(tmp_path)
    other_doc_id = run(_seed_other)

    body = assert_envelope(
        client.delete(f"/api/v1/documents/delete/{other_doc_id}", headers=logged_in["headers"]), 404
    )
    assert body["msg"] == "文档不存在"

    async def _still_there(session):
        from sqlalchemy import select as _select

        return await session.scalar(_select(Document).where(Document.id == _u(other_doc_id)))

    assert run(_still_there) is not None


def test_delete_requires_login(client):
    assert_envelope(client.delete("/api/v1/documents/delete/1f5a5f9c-3f9e-4b1a-9e2a-7cb0a9d00001"), 401)


def test_delete_invalid_document_id_is_422(client, logged_in):
    """需求 7-9：非 uuid / 空值都在边界处被挡掉，不会进业务逻辑"""
    assert_envelope(client.delete("/api/v1/documents/delete/not-a-uuid", headers=logged_in["headers"]), 422)
    assert_envelope(client.delete("/api/v1/documents/delete/123", headers=logged_in["headers"]), 422)


def test_delete_twice_returns_404(client, logged_in, run, monkeypatch, tmp_path):
    doc_id, _ = seed_document_with_file(run, logged_in, monkeypatch, tmp_path)
    assert_envelope(client.delete(f"/api/v1/documents/delete/{doc_id}", headers=logged_in["headers"]), 200)
    assert_envelope(client.delete(f"/api/v1/documents/delete/{doc_id}", headers=logged_in["headers"]), 404)


def test_service_delete_document_file_helper(tmp_path, monkeypatch):
    """删除逻辑：命中 True / 不在 False（不抛异常）/ 两个分支都能复算定位 / 非法段拒绝拼路径"""
    import uuid as uuid_lib

    monkeypatch.chdir(tmp_path)
    owner, other, kb_id, doc_id = (str(uuid_lib.uuid4()) for _ in range(4))

    # 库主人自己传的 → 个人目录 data/{owner}/{kb_id}/
    own_dir = tmp_path / "data" / owner / kb_id
    own_dir.mkdir(parents=True)
    (own_dir / f"{doc_id}.md").write_text("x")
    assert delete_document_file(owner, owner, kb_id, doc_id, "md") is True
    assert not (own_dir / f"{doc_id}.md").exists()
    assert delete_document_file(owner, owner, kb_id, doc_id, "md") is False

    # 被授权人传的 → data/share/{kb_id}/，用 (库owner, 上传者) 复算同样能定位
    share_dir = tmp_path / "data" / "share" / kb_id
    share_dir.mkdir(parents=True)
    (share_dir / f"{doc_id}.md").write_text("y")
    assert delete_document_file(owner, other, kb_id, doc_id, "md") is True
    assert not (share_dir / f"{doc_id}.md").exists()

    # 拼路径前的字符校验：非法 uuid / 非法扩展名一律拒绝，绝不去拼目录
    with pytest.raises(ValueError):
        delete_document_file(owner, other, "../../etc", doc_id, "md")
    with pytest.raises(ValueError):
        delete_document_file(owner, other, kb_id, doc_id, "md/../sh")

def make_kb(run, user_id, name="上传目标库"):
    """建一个属于该用户的知识库，返回 kb_id（str）"""

    async def _seed(session):
        kb = await _add_kb(session, user_id, name=name)
        return str(kb.id)

    return run(_seed)


def upload(client, headers, *, kb_id, content=b"# doc body\nfirst line", filename="report.md", name=None):
    params = {"kb_id": kb_id}
    if name is not None:
        params["document_name"] = name
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": (filename, content, "application/octet-stream")},
        params=params,
    )


def uploaded_files(tmp_path, kb_owner_id=None, kb_id=None):
    """返回文件名列表

    - 传 (kb_owner_id, kb_id)：只看这个库的**两个**目录（库主人目录 + data/share/）；
      少扫一个就会把协作上传的文件误判成 orphan，这一点在 /status 那边同理。
    - 不传：扫整个 data/，用来断言「失败回滚后没留垃圾」。
    """
    if kb_id is None:
        roots = [tmp_path / "data"]
    else:
        roots = [
            tmp_path / "data" / str(kb_owner_id) / str(kb_id),
            tmp_path / "data" / "share" / str(kb_id),
        ]
    names = []
    for root in roots:
        if root.exists():
            names.extend(item.name for item in root.rglob("*") if item.is_file())
    return sorted(set(names))


def test_upload_success(client, logged_in, run, engine, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])

    body = assert_envelope(
        upload(client, logged_in["headers"], kb_id=kb_id, content=b"# hello md", filename="报告.md"), 200
    )
    data = body["data"]
    assert body["msg"] == "上传成功"
    assert data["status"] == "pending"
    assert data["title"] == "报告"                      # 默认取原文件名去扩展名
    assert data["filename"] == "报告.md"
    assert data["file_size"] == len(b"# hello md")
    assert data["mime_type"] == "text/markdown"          # 客户端传的 Content-Type 不信，按扩展名派生
    assert data["kb_id"] == kb_id
    assert data["download_url"].endswith(data["id"])

    # 落盘按约定：data/{user_id}/{document_id}.{ext}
    on_disk = tmp_path / "data" / logged_in["user_id"] / kb_id / f"{data['id']}.md"
    assert on_disk.exists() and on_disk.read_bytes() == b"# hello md"
    assert uploaded_files(tmp_path, logged_in["user_id"], kb_id) == [f"{data['id']}.md"]

    # 知识库版本信息同步更新
    async def _kb(session):
        return await session.get(KnowledgeBase, _u(kb_id))

    kb = run(_kb)
    assert (kb.version, kb.document_count) == (2, 1)

    # 列表里能查到
    listed = assert_envelope(client.get("/api/v1/documents/list", headers=logged_in["headers"]), 200)
    assert [i["id"] for i in listed["data"]["items"]] == [data["id"]]


def test_upload_document_name_is_used_as_title(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    body = assert_envelope(
        upload(client, logged_in["headers"], kb_id=kb_id, filename="x.md", name="季度总结"), 200
    )
    assert body["data"]["title"] == "季度总结"
    assert body["data"]["filename"] == "x.md"


def test_upload_rejects_duplicate_in_same_kb(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    content = b"duplicated content"

    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=content), 200)
    body = assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=content), 409)
    assert "已在此知识库" in body["msg"]

    # 判重在写盘之前，所以磁盘上只会有一个文件
    assert len(uploaded_files(tmp_path, logged_in["user_id"], kb_id)) == 1


def test_upload_allows_same_file_in_another_kb(client, logged_in, run, monkeypatch, tmp_path):
    """判重口径是 (kb_id, file_hash)，换个库放同一个文件是允许的"""
    monkeypatch.chdir(tmp_path)
    kb_a = make_kb(run, logged_in["user_id"], name="库A")
    kb_b = make_kb(run, logged_in["user_id"], name="库B")
    content = b"same bytes"

    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_a, content=content), 200)
    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_b, content=content), 200)
    assert len(uploaded_files(tmp_path, logged_in["user_id"], kb_a)) + len(uploaded_files(tmp_path, logged_in["user_id"], kb_b)) == 2


@pytest.mark.parametrize("filename", ["tool.exe", "README", "a.pdf.md.exe", "no_ext.", "x.md/../y"])
def test_upload_rejects_bad_extension(client, logged_in, run, monkeypatch, tmp_path, filename):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    body = assert_envelope(
        upload(client, logged_in["headers"], kb_id=kb_id, filename=filename, content=b"x"), 400
    )
    assert "类型" in body["msg"] or "扩展名" in body["msg"]
    assert uploaded_files(tmp_path, logged_in["user_id"]) == []  # 边界不过，一个字节都不写


def test_upload_sanitizes_windows_path_filename(client, logged_in, run, monkeypatch, tmp_path):
    """客户端可能把整条本地路径当 filename 传上来，展示名只取最后一段，落盘名一律用 document_id"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    body = assert_envelope(
        upload(client, logged_in["headers"], kb_id=kb_id, filename=r"C:\Users\me\文档.pdf", content=b"%PDF-1.4"),
        200,
    )
    assert body["data"]["filename"] == "文档.pdf"
    assert body["data"]["title"] == "文档"
    assert uploaded_files(tmp_path, logged_in["user_id"], kb_id) == [f"{body['data']['id']}.pdf"]


def test_upload_rejects_other_users_kb(client, logged_in, other_headers, run, monkeypatch, tmp_path):
    """越权：kb_id 是客户端传的，必须校验归属，否则能把文件塞进别人的知识库"""
    monkeypatch.chdir(tmp_path)

    async def _seed_other(session):
        from sqlalchemy import select as _select

        from app.db.models import User

        result = await session.execute(_select(User).where(User.username == "lisi"))
        lisi = result.scalar_one()
        return str((await _add_kb(session, lisi.id, name="lisi的库")).id)

    lisi_kb_id = run(_seed_other)
    assert_envelope(upload(client, logged_in["headers"], kb_id=lisi_kb_id, content=b"hack"), 404)
    assert uploaded_files(tmp_path, logged_in["user_id"]) == []


def test_upload_unknown_kb_is_404(client, logged_in, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert_envelope(
        upload(client, logged_in["headers"], kb_id="11111111-1111-1111-1111-111111111111", content=b"x"), 404
    )


def test_upload_invalid_kb_id_is_404(client, logged_in, monkeypatch, tmp_path):
    """非法 kb_id 也不能退化成「不过滤」，直接按不存在处理"""
    monkeypatch.chdir(tmp_path)
    assert_envelope(upload(client, logged_in["headers"], kb_id="not-a-uuid", content=b"x"), 404)


def test_upload_too_large_is_413_and_leaves_no_file(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(documentrouter, "MAX_UPLOAD_BYTES", 16)  # 压到 16 字节，测试里好触发
    kb_id = make_kb(run, logged_in["user_id"])

    body = assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=b"x" * 200), 413)
    assert "超过大小限制" in body["msg"]
    assert uploaded_files(tmp_path, logged_in["user_id"]) == []


def test_upload_requires_login(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    resp = client.post(
        "/api/v1/documents/upload",
        files={"file": ("a.md", b"x", "text/markdown")},
        params={"kb_id": "11111111-1111-1111-1111-111111111111"},
    )
    assert_envelope(resp, 401)


def test_upload_without_file_field_is_422(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    assert_envelope(
        client.post("/api/v1/documents/upload", headers=logged_in["headers"], params={"kb_id": kb_id}), 422
    )


def test_upload_db_failure_rolls_back_the_file(client, logged_in, run, monkeypatch, tmp_path):
    """写库失败必须把刚落盘的文件删掉，不留垃圾文件"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])

    async def _boom(**kwargs):
        raise RuntimeError("数据库炸了")

    monkeypatch.setattr(document_crud, "create_document", _boom)
    body = assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=b"x"), 500)
    assert "回滚" in body["msg"]
    assert uploaded_files(tmp_path, logged_in["user_id"]) == []


# ---------------- GET /documents/status ----------------
def test_status_empty_kb(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"], name="空库")
    body = assert_envelope(
        client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=logged_in["headers"]), 200
    )
    data = body["data"]
    assert body["msg"] == "知识库目前没有文件"          # 空库不是异常
    assert (data["document_total"], data["disk_file_count"], data["healthy"]) == (0, 0, True)
    # 五种状态都会补齐，前端不用猜键
    assert set(data["status_counts"]) == {"pending", "parsing", "chunking", "indexed", "failed"}
    assert data["kb_version"] == 1


def test_status_healthy(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path)
    body = assert_envelope(
        client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=logged_in["headers"]), 200
    )
    data = body["data"]
    assert data["healthy"] is True
    assert (data["document_total"], data["disk_file_count"]) == (1, 1)
    assert data["missing_files"] == [] and data["orphan_files"] == []
    assert data["status_counts"]["pending"] == 1


def test_status_detects_missing_file(client, logged_in, run, monkeypatch, tmp_path):
    """库里有记录、磁盘文件丢了 → missing_files（文档打不开），healthy=false，但接口仍是 200"""
    monkeypatch.chdir(tmp_path)
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path)
    (tmp_path / canonical_file_path(logged_in["user_id"], kb_id, doc_id)).unlink()

    body = assert_envelope(
        client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=logged_in["headers"]), 200
    )
    data = body["data"]
    assert data["healthy"] is False
    assert data["missing_files"] == [f"{doc_id}.md"]
    assert "不一致" in body["msg"]


def test_status_detects_orphan_file(client, logged_in, run, monkeypatch, tmp_path):
    """磁盘上有文件、库里没记录 → orphan_files（占空间的垃圾）"""
    monkeypatch.chdir(tmp_path)
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path)
    orphan_dir = tmp_path / "data" / logged_in["user_id"] / kb_id
    (orphan_dir / "11111111-1111-1111-1111-111111111111.md").write_text("垃圾")

    data = assert_envelope(
        client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=logged_in["headers"]), 200
    )["data"]
    assert data["healthy"] is False
    assert data["orphan_files"] == ["11111111-1111-1111-1111-111111111111.md"]
    assert data["document_total"] == 1 and data["disk_file_count"] == 2


def test_status_kb_not_found(client, logged_in, monkeypatch, tmp_path):
    """原来的写法这里会 TypeError：kb 是 ORM 对象，不能 len()，也拿不到别人的库"""
    monkeypatch.chdir(tmp_path)
    assert_envelope(
        client.get("/api/v1/documents/status?kb_id=22222222-2222-2222-2222-222222222222", headers=logged_in["headers"]),
        404,
    )
    assert_envelope(client.get("/api/v1/documents/status?kb_id=abc", headers=logged_in["headers"]), 404)


def test_status_other_users_kb_is_404(client, logged_in, other_headers, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def _seed_other(session):
        from sqlalchemy import select as _select

        from app.db.models import User

        lisi = (await session.execute(_select(User).where(User.username == "lisi"))).scalar_one()
        return str((await _add_kb(session, lisi.id, name="lisi的库")).id)

    lisi_kb = run(_seed_other)
    assert_envelope(
        client.get(f"/api/v1/documents/status?kb_id={lisi_kb}", headers=logged_in["headers"]), 404
    )


def test_status_requires_login(client):
    assert_envelope(client.get("/api/v1/documents/status?kb_id=11111111-1111-1111-1111-111111111111"), 401)


# ---------------- GET /documents/detail ----------------
def test_detail_without_chunks(client, logged_in, run, monkeypatch, tmp_path):
    """M2 还没做，块为空：preview=null + chunk_total=0，不能算错误"""
    monkeypatch.chdir(tmp_path)
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path, chunks=0)

    body = assert_envelope(
        client.get(f"/api/v1/documents/detail?document_id={doc_id}", headers=logged_in["headers"]), 200
    )
    data = body["data"]
    assert data["id"] == doc_id and data["kb_id"] == kb_id
    assert data["content_preview"] is None
    assert data["chunk_total"] == 0
    assert data["kb_name"] == "待删库"
    assert data["kb_version"] == 1


def test_detail_returns_preview_in_seq_order(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path, chunks=3)

    async def _ordered(session):
        # 故意让 seq 与插入顺序无关：按 seq 升序拼接正文
        from sqlalchemy import select as _select

        from app.db.models import Chunk

        rows = (await session.execute(_select(Chunk).where(Chunk.document_id == _u(doc_id)).order_by(Chunk.seq.desc()))).scalars().all()
        return [row.content for row in rows]

    assert run(_ordered) == ["块2", "块1", "块0"]  # 磁盘/库里插入顺序是反的，详情必须按 seq 升序

    data = assert_envelope(
        client.get(f"/api/v1/documents/detail?document_id={doc_id}", headers=logged_in["headers"]), 200
    )["data"]
    assert data["content_preview"] == "块0\n\n块1\n\n块2"
    assert data["chunk_total"] == 3


def test_detail_other_users_document_is_404(client, logged_in, other_headers, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def _seed_other(session):
        from sqlalchemy import select as _select

        from app.db.models import User

        lisi = (await session.execute(_select(User).where(User.username == "lisi"))).scalar_one()
        kb = await _add_kb(session, lisi.id, name="lisi的库")
        return str((await _add_doc(session, kb.id, lisi.id, filename="机密.md", file_hash="机密-hash")).id)

    other_doc = run(_seed_other)
    assert_envelope(
        client.get(f"/api/v1/documents/detail?document_id={other_doc}", headers=logged_in["headers"]), 404
    )


def test_detail_requires_uuid(client, logged_in):
    assert_envelope(client.get("/api/v1/documents/detail?document_id=abc", headers=logged_in["headers"]), 422)
    assert_envelope(client.get("/api/v1/documents/detail", headers=logged_in["headers"]), 422)


# ---------------- GET /documents/download/{document_id} ----------------
def test_download_returns_the_file(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path)

    resp = client.get(f"/api/v1/documents/download/{doc_id}", headers=logged_in["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.content == "正文内容".encode()
    disposition = resp.headers["content-disposition"]
    assert "要删的文档.md" in disposition or "filename" in disposition  # 用原始名，不是 document_id.md
    assert resp.headers["content-type"].startswith("text/markdown")


def test_download_missing_file_is_404(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    doc_id, kb_id = seed_document_with_file(run, logged_in, monkeypatch, tmp_path, with_file=False)
    body = assert_envelope(
        client.get(f"/api/v1/documents/download/{doc_id}", headers=logged_in["headers"]), 404
    )
    assert "文件已丢失" in body["msg"]


def test_download_other_users_file_is_404(client, logged_in, other_headers, run, monkeypatch, tmp_path):
    """越权：拿别人的 document_id 下载不到，也不泄露归属"""
    monkeypatch.chdir(tmp_path)

    async def _seed_other(session):
        from sqlalchemy import select as _select

        from app.db.models import User

        lisi = (await session.execute(_select(User).where(User.username == "lisi"))).scalar_one()
        kb = await _add_kb(session, lisi.id, name="lisi的库")
        doc = await _add_doc(session, kb.id, lisi.id, filename="机密.md", file_hash="lisi-dl")
        return str(doc.id), str(doc.kb_id), str(lisi.id)

    other_id, other_kb, other_uid = run(_seed_other)
    target = tmp_path / canonical_file_path(other_uid, other_kb, other_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("别人的内容")

    assert_envelope(
        client.get(f"/api/v1/documents/download/{other_id}", headers=logged_in["headers"]), 404
    )
    assert target.exists()  # 没被碰过


def test_download_rejects_path_outside_user_dir(client, logged_in, run, monkeypatch, tmp_path):
    """file_path 被写成跳出本人目录时，不返回文件内容"""
    monkeypatch.chdir(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("机密")
    uid = logged_in["user_id"]

    async def _seed(session):
        kb = await _add_kb(session, uid, name="脏路径库")
        doc = await _add_doc(session, kb.id, uid, filename="x.md", file_hash="dirty-hash")
        doc.file_path = f"../secret.txt"   # 跳出 data/ 的相对路径
        await session.commit()
        return str(doc.id)

    doc_id = run(_seed)
    resp = client.get(f"/api/v1/documents/download/{doc_id}", headers=logged_in["headers"])
    assert resp.status_code == 403, resp.text
    assert "机密" not in resp.text


def test_legacy_layout_is_readable_but_not_deleted(client, logged_in, run, monkeypatch, tmp_path):
    """老布局遗留（data/kb/...）：仍能下载（读比删宽松）；删除只删记录、不碰磁盘

    注意 /status 的 orphan_files 只扫当前约定的两个目录，所以放在 data/kb/ 里的历史文件
    不会被报出来 —— 这类遗留要靠一次性清理脚本处理，不属于接口职责。
    """
    kb_id = make_kb(run, logged_in["user_id"], name="遗留布局库")

    async def _seed(session):
        doc = await _add_doc(
            session, _u(kb_id), _u(logged_in["user_id"]), filename="遗留.md", file_hash="legacy-hash"
        )
        doc.file_path = f"data/kb/{kb_id}/{doc.id}.md"     # 上一轮废弃的约定
        await session.commit()
        return str(doc.id)

    monkeypatch.chdir(tmp_path)
    doc_id = run(_seed)
    legacy = tmp_path / "data" / "kb" / kb_id / f"{doc_id}.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("旧位置的内容")

    dl = client.get(f"/api/v1/documents/download/{doc_id}", headers=logged_in["headers"])
    assert dl.status_code == 200 and dl.text == "旧位置的内容"

    status = assert_envelope(
        client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=logged_in["headers"]), 200
    )["data"]
    assert status["missing_files"] == []      # 按 file_path 精判：文件在，只是位置是老的
    assert status["orphan_files"] == []       # 不在约定目录里，扫不到也不乱报

    body = assert_envelope(
        client.delete(f"/api/v1/documents/delete/{doc_id}", headers=logged_in["headers"]), 200
    )
    assert "存储路径不合法" in body["msg"]
    assert legacy.exists()                     # 不按新规则乱删老文件
