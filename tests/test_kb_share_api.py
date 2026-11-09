"""知识库共享授权测试：3 条路由 + 权限矩阵 + 文档路由的越权行为"""

import uuid

import pytest
from sqlalchemy import select as sa_select

from app.db.models import Chunk, Document, KnowledgeBase
from tests.test_document_api import (
    assert_envelope,
    make_kb,
    own_file_path,
    share_file_path,
    uploaded_files,
    upload,
)
from tests.test_schema_models import _add_doc, _add_kb, _u


def envelope(resp, status):
    return assert_envelope(resp, status)


def account(client, username: str, email: str) -> dict:
    """注册 + 登录，返回 {"headers","user_id","email"}"""
    client.post(
        "/api/v1/user/register",
        json={"username": username, "email": email, "password": "abc123456"},
    )
    data = client.post(
        "/api/v1/user/login", json={"account": username, "password": "abc123456"}
    ).json()["data"]
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "user_id": data["user"]["id"],
        "email": data["user"]["email"],
    }


@pytest.fixture()
def owner(client) -> dict:
    return account(client, "zhangsan", "zhangsan@example.com")


@pytest.fixture()
def reader(client) -> dict:
    return account(client, "lisi", "lisi@example.com")


@pytest.fixture()
def stranger(client) -> dict:
    return account(client, "wangwu", "wangwu@example.com")


def share(client, who, kb_id, target_email, permission="read"):
    return client.post(
        "/api/v1/kb/share",
        headers=who["headers"],
        params={"kb_id": kb_id},
        json={"email": target_email, "permission": permission},
    )


def unshare(client, who, kb_id, target_email):
    return client.delete(
        "/api/v1/kb/share", headers=who["headers"], params={"kb_id": kb_id, "email": target_email}
    )


def share_list(client, who, kb_id):
    return client.get("/api/v1/kb/share/list", headers=who["headers"], params={"kb_id": kb_id})


def seed_doc(run, tmp_path, owner_id, kb_id, monkeypatch, *, content="# 库主的内容", ext="md"):
    """在 owner 的库里放一个文档 + 磁盘文件（目录主人 = 库 owner）"""
    monkeypatch.chdir(tmp_path)

    async def _seed(session):
        doc = await _add_doc(session, kb_id, owner_id, filename="库主的文档.md", file_hash=f"h-{uuid.uuid4().hex}")
        doc.file_path = own_file_path(owner_id, kb_id, doc.id)
        await session.commit()
        session.add(Chunk(document_id=doc.id, kb_id=_u(kb_id), owner_id=_u(owner_id), seq=0, content=content))
        await session.commit()
        return str(doc.id)

    doc_id = run(_seed)
    target = tmp_path / own_file_path(owner_id, kb_id, doc_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return doc_id


# ---------------- 授权本身 ----------------
def test_grant_read_and_list(client, owner, reader, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")

    body = envelope(share(client, owner, kb_id, reader["email"], "read"), 200)
    data = body["data"]
    assert data["kb_id"] == kb_id
    assert data["grantee_email"] == reader["email"]
    assert data["grantee_username"] == "lisi"
    assert data["permission"] == "read"

    listed = envelope(share_list(client, owner, kb_id), 200)["data"]
    assert [item["grantee_email"] for item in listed] == [reader["email"]]


def test_grant_is_idempotent_and_upgradable(client, owner, reader, run, monkeypatch, tmp_path):
    """重复授权 = 更新权限，不会多出记录"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")

    first = envelope(share(client, owner, kb_id, reader["email"], "read"), 200)["data"]
    second = envelope(share(client, owner, kb_id, reader["email"], "write"), 200)["data"]
    assert first["share_id"] == second["share_id"]
    assert second["permission"] == "write"
    assert len(envelope(share_list(client, owner, kb_id), 200)["data"]) == 1


def test_only_owner_can_grant(client, owner, reader, stranger, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    envelope(share(client, owner, kb_id, reader["email"], "write"), 200)

    # write 被授权人也不能再往下授权（防止权限人传人失控）
    body = envelope(share(client, reader, kb_id, stranger["email"], "read"), 403)
    assert "创建者" in body["msg"]

    # 完全无关的人 → 连读都没有，按「不存在」处理，不暴露这个库存在
    assert envelope(share(client, stranger, kb_id, reader["email"], "read"), 404)["status"] == 404


def test_grant_edge_cases(client, owner, reader, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")

    assert envelope(share(client, owner, kb_id, owner["email"], "read"), 400)["status"] == 400
    assert envelope(share(client, owner, kb_id, "ghost@x.com", "read"), 404)["status"] == 404
    assert envelope(share(client, owner, kb_id, reader["email"], "delete"), 422)["status"] == 422
    # 非法 kb_id 与「不存在」同一响应（404），不告诉调用方到底是格式错还是没这个库；
    # 400 只留给「压根没传 kb_id」
    assert envelope(share(client, owner, "not-a-uuid", reader["email"], "read"), 404)["status"] == 404
    resp = client.post("/api/v1/kb/share", headers=owner["headers"], json={"email": reader["email"]})
    assert envelope(resp, 400)["status"] == 400


def test_revoke(client, owner, reader, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    envelope(share(client, owner, kb_id, reader["email"], "read"), 200)

    body = envelope(unshare(client, owner, kb_id, reader["email"]), 200)
    assert body["data"] is None
    assert envelope(share_list(client, owner, kb_id), 200)["data"] == []

    # 幂等：重复取消不算错误
    assert "本来就没有" in envelope(unshare(client, owner, kb_id, reader["email"]), 200)["msg"]


def test_share_requires_login(client, owner, reader, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    resp = client.post("/api/v1/kb/share", params={"kb_id": kb_id}, json={"email": reader["email"]})
    assert resp.status_code == 401


# ---------------- 权限矩阵：read ----------------
def test_reader_can_see_list_detail_download(client, owner, reader, run, monkeypatch, tmp_path):
    """只读授权：能看库主的文档列表 / 详情 / 下载，但不能写"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    doc_id = seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)
    envelope(share(client, owner, kb_id, reader["email"], "read"), 200)

    listed = envelope(client.get(f"/api/v1/documents/list?kb_id={kb_id}", headers=reader["headers"]), 200)["data"]
    assert [item["id"] for item in listed["items"]] == [doc_id]

    detail = envelope(
        client.get(f"/api/v1/documents/detail?document_id={doc_id}", headers=reader["headers"]), 200
    )["data"]
    assert detail["content_preview"] == "# 库主的内容"
    assert detail["kb_name"] == "团队资料"

    dl = client.get(f"/api/v1/documents/download/{doc_id}", headers=reader["headers"])
    assert dl.status_code == 200 and dl.text == "# 库主的内容"

    # 只读不能上传、不能删
    assert "读写" in envelope(
        upload(client, reader["headers"], kb_id=kb_id, content=b"hack"), 403
    )["msg"]
    assert envelope(
        client.delete(f"/api/v1/documents/delete/{doc_id}", headers=reader["headers"]), 403
    )["status"] == 403


def test_reader_status_visible(client, owner, reader, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)

    # 未授权：404
    assert envelope(client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=reader["headers"]), 404)["status"] == 404
    envelope(share(client, owner, kb_id, reader["email"], "read"), 200)
    data = envelope(client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=reader["headers"]), 200)["data"]
    assert data["healthy"] is True and data["document_total"] == 1


# ---------------- 权限矩阵：write ----------------
def test_writer_uploads_into_share_dir(client, owner, reader, run, engine, monkeypatch, tmp_path):
    """write 授权：能往库里上传；文件落 data/share/{kb_id}/，不写进库主人的个人目录"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    envelope(share(client, owner, kb_id, reader["email"], "write"), 200)

    data = envelope(upload(client, reader["headers"], kb_id=kb_id, content=b"# shared contribution"), 200)["data"]
    assert data["kb_id"] == kb_id
    assert uploaded_files(tmp_path, owner["user_id"], kb_id) == [f"{data['id']}.md"]  # 只有 share 目录里那一份

    # 库主人在自己的列表里能看到被授权人传的文档
    listed = envelope(client.get(f"/api/v1/documents/list?kb_id={kb_id}", headers=owner["headers"]), 200)["data"]
    assert [item["id"] for item in listed["items"]] == [data["id"]]   # 库主人看得见别人共享上传的内容


def test_writer_can_delete_others_document(client, owner, reader, run, monkeypatch, tmp_path):
    """write ⊃ 删除（含别人传的）：团队库语义，改库设置/再授权仍然 owner 专属"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    doc_id = seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)
    envelope(share(client, owner, kb_id, reader["email"], "write"), 200)

    assert envelope(client.delete(f"/api/v1/documents/delete/{doc_id}", headers=reader["headers"]), 200)
    assert uploaded_files(tmp_path, owner["user_id"], kb_id) == []

    async def _gone(session):
        return await session.scalar(sa_select(Document).where(Document.id == _u(doc_id)))

    assert run(_gone) is None


def test_writer_still_cannot_grant(client, owner, reader, stranger, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    envelope(share(client, owner, kb_id, reader["email"], "write"), 200)
    assert envelope(share(client, reader, kb_id, stranger["email"], "read"), 403)["status"] == 403


# ---------------- public 库 ----------------
def test_public_kb_is_read_only_for_everyone(client, owner, stranger, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def _seed_public(session):
        kb = await _add_kb(session, owner["user_id"], name="公开资料", description="所有人都能读", visibility="public")
        return str(kb.id)

    kb_id = run(_seed_public)
    doc_id = seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)

    listed = envelope(client.get(f"/api/v1/documents/list?kb_id={kb_id}", headers=stranger["headers"]), 200)["data"]
    assert [item["id"] for item in listed["items"]] == [doc_id]
    assert envelope(upload(client, stranger["headers"], kb_id=kb_id, content=b"x"), 403)["status"] == 403
    assert envelope(share(client, stranger, kb_id, owner["email"], "read"), 403)["status"] == 403


def test_stranger_cannot_touch_private_kb(client, owner, stranger, run, monkeypatch, tmp_path):
    """完全无关的人：私有库的文档一律 404，既不泄露存在性也拿不到内容"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="私人库")
    doc_id = seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)

    assert envelope(client.get(f"/api/v1/documents/detail?document_id={doc_id}", headers=stranger["headers"]), 404)["status"] == 404
    assert envelope(client.get(f"/api/v1/documents/download/{doc_id}", headers=stranger["headers"]), 404)["status"] == 404
    assert envelope(client.delete(f"/api/v1/documents/delete/{doc_id}", headers=stranger["headers"]), 404)["status"] == 404
    assert envelope(client.get(f"/api/v1/documents/list?kb_id={kb_id}", headers=stranger["headers"]), 200)["data"]["total"] == 0
    assert uploaded_files(tmp_path, owner["user_id"], kb_id) == [f"{doc_id}.md"]  # 没被碰


def test_delete_own_document_in_shared_kb_still_works(client, owner, reader, run, monkeypatch, tmp_path):
    """owner 自己删自己库里的文档不受影响（回归保护）"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="团队资料")
    doc_id = seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)
    envelope(share(client, owner, kb_id, reader["email"], "read"), 200)
    assert envelope(client.delete(f"/api/v1/documents/delete/{doc_id}", headers=owner["headers"]), 200)


def test_kb_delete_cascades_shares(run, owner):
    """删库要连授权记录一起清掉（FK CASCADE），不留孤儿授权"""
    from app.db.crud import kb_crud
    from app.db.models import KbShare
    from sqlalchemy import func

    async def _seed(session):
        kb = await _add_kb(session, owner["user_id"], name="要删的库")
        share_obj = await kb_crud.grant_share(kb.id, _u(owner["user_id"]), None, session, permission="write")
        return str(kb.id), share_obj is not None

    kb_id, granted = run(_seed)
    assert granted

    async def _delete(session):
        kb = await session.get(KnowledgeBase, _u(kb_id))
        await session.delete(kb)
        await session.commit()
        return await session.scalar(sa_select(func.count()).select_from(KbShare).where(KbShare.kb_id == _u(kb_id)))

    assert run(_delete) == 0


def test_owner_and_sharee_files_live_in_two_dirs(client, owner, reader, run, monkeypatch, tmp_path):
    """同一个库的两个目录并存：都能列、都能读、都能删，/status 也不会误报"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="协作库")
    owner_doc = seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)      # → data/{owner}/{kb}/
    envelope(share(client, owner, kb_id, reader["email"], "write"), 200)
    shared_doc = envelope(
        upload(client, reader["headers"], kb_id=kb_id, content=b"contributed"), 200
    )["data"]["id"]

    assert (tmp_path / own_file_path(owner["user_id"], kb_id, owner_doc)).exists()
    assert (tmp_path / share_file_path(kb_id, shared_doc)).exists()
    assert sorted(uploaded_files(tmp_path, owner["user_id"], kb_id)) == sorted([f"{owner_doc}.md", f"{shared_doc}.md"])
    # 两份文件分别在两个不同目录下，谁也没写进谁的私人大目录
    assert (tmp_path / own_file_path(owner["user_id"], kb_id, owner_doc)).exists()
    assert (tmp_path / share_file_path(kb_id, shared_doc)).exists()
    assert not (tmp_path / share_file_path(kb_id, owner_doc)).exists()
    assert not (tmp_path / own_file_path(owner["user_id"], kb_id, shared_doc)).exists()

    # 库主人视角：两条都在列表里
    listed = envelope(
        client.get(f"/api/v1/documents/list?kb_id={kb_id}", headers=owner["headers"]), 200
    )["data"]
    assert {item["id"] for item in listed["items"]} == {owner_doc, shared_doc}

    # 被授权人能读能下载库主人传的文件（路径在别人目录下，读权限由库权限决定）
    dl = client.get(f"/api/v1/documents/download/{owner_doc}", headers=reader["headers"])
    assert dl.status_code == 200

    # 对账：两个目录都要覆盖，否则 data/share 里的文件会被误判成 orphan
    status = envelope(
        client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=owner["headers"]), 200
    )["data"]
    assert status["healthy"] is True, status
    assert status["orphan_files"] == [] and status["missing_files"] == []
    assert status["document_total"] == 2 and status["disk_file_count"] == 2


def test_delete_from_share_dir_only_touches_share(client, owner, reader, run, monkeypatch, tmp_path):
    """删被授权人传的那份：只动 data/share 里的文件，库主人目录不受影响"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="协作库")
    owner_doc = seed_doc(run, tmp_path, owner["user_id"], kb_id, monkeypatch)
    envelope(share(client, owner, kb_id, reader["email"], "write"), 200)
    shared_doc = envelope(upload(client, reader["headers"], kb_id=kb_id, content=b"c"), 200)["data"]["id"]

    assert envelope(client.delete(f"/api/v1/documents/delete/{shared_doc}", headers=owner["headers"]), 200)
    assert not (tmp_path / share_file_path(kb_id, shared_doc)).exists()
    assert (tmp_path / own_file_path(owner["user_id"], kb_id, owner_doc)).exists()
    assert uploaded_files(tmp_path, owner["user_id"], kb_id) == [f"{owner_doc}.md"]


def test_orphan_in_share_dir_is_detected(client, owner, reader, run, monkeypatch, tmp_path):
    """垃圾文件出现在 data/share 里也必须被抓出来（只扫一个目录就会漏）"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, owner["user_id"], name="协作库")
    envelope(share(client, owner, kb_id, reader["email"], "write"), 200)
    ghost = tmp_path / share_file_path(kb_id, "44444444-4444-4444-4444-444444444444")
    ghost.parent.mkdir(parents=True)
    ghost.write_text("没入库的垃圾")

    status = envelope(
        client.get(f"/api/v1/documents/status?kb_id={kb_id}", headers=owner["headers"]), 200
    )["data"]
    assert status["healthy"] is False
    assert status["orphan_files"] == ["44444444-4444-4444-4444-444444444444.md"]
