"""文档接口测试：GET /documents/list + 尚未实现的占位接口"""

from app.db.models import KnowledgeBase
from app.db.crud import document_crud

from tests.test_schema_models import _add_doc, _add_kb  # 复用建数据的辅助函数


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


def test_not_implemented_endpoints_return_501(client, logged_in):
    """还没做的接口要给出明确的 501，而不是静默返回 null 骗过前端"""
    headers = logged_in["headers"]
    cases = [
        ("post", "/api/v1/documents/upload"),
        ("get", "/api/v1/documents/detail"),
        ("get", "/api/v1/documents/status"),
        ("delete", "/api/v1/documents/delete/1f5a5f9c-3f9e-4b1a-9e2a-7cb0a9d00001"),
        ("get", "/api/v1/documents/download/1f5a5f9c-3f9e-4b1a-9e2a-7cb0a9d00001"),
    ]
    for method, path in cases:
        resp = getattr(client, method)(path, headers=headers)
        body = assert_envelope(resp, 501)
        assert "TODO.md" in body["msg"]


def test_create_document_bumps_kb_version(run, logged_in):
    """CRUD 层：新增/删除文档都会重算知识库统计并 +1 版本号（就是你说的「更新知识库版本信息」）"""
    uid = logged_in["user_id"]

    async def _make_kb(session):
        kb = await _add_kb(session, uid, name="产品库")
        return str(kb.id)

    kb_id = run(_make_kb)

    async def _create(session):
        doc = await document_crud.create_document(
            kb_id, uid, "需求说明书", "需求说明书.pdf", f"data/{uid}/documents/x.pdf", 2048, "application/pdf", "hash-1", session
        )
        kb = await session.get(KnowledgeBase, __import__("uuid").UUID(kb_id))
        return doc, kb.version, kb.document_count

    doc, version, doc_count = run(_create)
    assert doc is not None and version == 2 and doc_count == 1

    async def _delete(session):
        target = await document_crud.get_document(doc.id, uid, session)
        ok = await document_crud.delete_document(target, session)
        kb = await session.get(KnowledgeBase, __import__("uuid").UUID(kb_id))
        return ok, kb.version, kb.document_count, kb.last_indexed_at is not None

    ok, version, doc_count, indexed = run(_delete)
    assert (ok, version, doc_count, indexed) == (True, 3, 0, True)


def test_duplicate_upload_is_rejected(run, logged_in):
    """同库同文件第二次入库返回 None（唯一索引兜住），路由层据此给 409"""
    uid = logged_in["user_id"]

    async def _seed(session):
        kb = await _add_kb(session, uid, name="重复库")
        first = await document_crud.create_document(
            kb.id, uid, "x.md", "x.md", "p", 10, "text/markdown", "same-hash", session
        )
        second = await document_crud.create_document(
            kb.id, uid, "x copy.md", "x copy.md", "p2", 10, "text/markdown", "same-hash", session
        )
        return first is not None, second is None

    assert run(_seed) == (True, True)
