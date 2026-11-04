"""表结构本身的测试：默认值、唯一约束、级联删除

这组用例保证「重新做的表」在真实读写下是对的，接口还没做完也不影响先验证。
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Chunk, DocStatus, Document, KbVisibility, KnowledgeBase, User, UserRole


def _u(value) -> uuid.UUID:
    """sa.Uuid 在 SQLite 上只认 uuid.UUID 对象（传 str 会报 'str' object has no attribute 'hex'）"""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def _add_user(session, username="zhangsan", email="zhangsan@example.com") -> User:
    user = User(username=username, email=email, hashed_password="hash")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _add_kb(session, owner_id, name="工作笔记", **kwargs) -> KnowledgeBase:
    kb = KnowledgeBase(owner_id=_u(owner_id), name=name, **kwargs)
    session.add(kb)
    await session.commit()
    await session.refresh(kb)
    return kb


async def _add_doc(session, kb_id, owner_id, filename="a.md", file_hash=None, **kwargs) -> Document:
    doc = Document(
        kb_id=_u(kb_id),
        owner_id=_u(owner_id),
        title=filename,
        filename=filename,
        file_path=f"data/{owner_id}/documents/{filename}",
        file_size=1024,
        mime_type="text/markdown",
        file_hash=file_hash or f"hash-{filename}",
        **kwargs,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


def test_column_defaults(run):
    """新表的默认值是否符合约定（role / visibility / status / version / extra）"""

    async def _check(session):
        user = await _add_user(session)
        kb = await _add_kb(session, user.id, description="存放产品需求文档，给写周报的人查")
        doc = await _add_doc(session, kb.id, user.id)
        chunk = Chunk(document_id=doc.id, kb_id=kb.id, owner_id=user.id, seq=0, content="第一段正文")
        session.add(chunk)
        await session.commit()
        await session.refresh(kb)
        await session.refresh(doc)
        await session.refresh(chunk)
        return user, kb, doc, chunk

    user, kb, doc, chunk = run(_check)

    assert user.role == UserRole.USER and user.is_admin is False
    assert user.is_active is True and user.deleted_at is None and user.updated_at is None
    assert kb.visibility == KbVisibility.PRIVATE and kb.version == 1
    assert kb.chunk_size == 800 and kb.chunk_overlap == 120
    assert kb.description.startswith("存放产品需求文档")
    assert kb.embedding_model is None and kb.embedding_dim is None  # 向量方案还没启用
    assert doc.status == DocStatus.PENDING and doc.chunk_count == 0
    assert doc.error_msg is None and doc.file_size == 1024
    assert chunk.extra == {} and chunk.token_count == 0 and chunk.page is None


def test_kb_name_unique_per_owner(run):
    """同一个人不能有两个同名知识库，但不同人可以用同一个名字"""

    async def _seed(session):
        a = await _add_user(session, "a1", "a1@x.com")
        b = await _add_user(session, "b2", "b2@x.com")
        await _add_kb(session, a.id, name="产品资料")
        return str(a.id), str(b.id)

    a_id, b_id = run(_seed)

    # 撞唯一索引之后当前 session 已经废了，所以每个断言各开一次 run()
    async def _duplicate(session):
        with pytest.raises(IntegrityError):
            await _add_kb(session, a_id, name="产品资料")

    async def _other_owner(session):
        kb = await _add_kb(session, b_id, name="产品资料")
        return kb.name

    run(_duplicate)
    assert run(_other_owner) == "产品资料"


def test_document_dedup_by_hash_in_same_kb(run):
    """同一个知识库里同一个文件（sha256）只能有一条记录；换库可以重复"""

    async def _seed(session):
        user = await _add_user(session)
        kb1 = await _add_kb(session, user.id, name="库一")
        kb2 = await _add_kb(session, user.id, name="库二")
        await _add_doc(session, kb1.id, user.id, filename="手册.pdf", file_hash="same-hash")
        return str(kb1.id), str(kb2.id), str(user.id)

    kb1_id, kb2_id, user_id = run(_seed)

    async def _duplicate(session):
        with pytest.raises(IntegrityError):
            await _add_doc(session, kb1_id, user_id, filename="手册(副本).pdf", file_hash="same-hash")

    async def _other_kb(session):
        doc = await _add_doc(session, kb2_id, user_id, filename="手册.pdf", file_hash="same-hash")
        return str(doc.id)

    run(_duplicate)
    assert run(_other_kb)


def test_chunk_seq_unique_per_document(run):
    """块序号在同一个文档内唯一（重索引时靠它保证幂等覆盖）"""

    async def _seed(session):
        user = await _add_user(session)
        kb = await _add_kb(session, user.id)
        doc = await _add_doc(session, kb.id, user.id)
        session.add(Chunk(document_id=doc.id, kb_id=kb.id, owner_id=user.id, seq=0, content="x"))
        await session.commit()
        return str(doc.id), str(kb.id), str(user.id)

    doc_id, kb_id, user_id = run(_seed)

    async def _duplicate(session):
        with pytest.raises(IntegrityError):
            session.add(
                Chunk(document_id=_u(doc_id), kb_id=_u(kb_id), owner_id=_u(user_id), seq=0, content="y")
            )
            await session.commit()

    run(_duplicate)


def test_delete_document_cascades_chunks(run):
    async def _check(session):
        user = await _add_user(session)
        kb = await _add_kb(session, user.id)
        doc = await _add_doc(session, kb.id, user.id)
        for seq in (0, 1, 2):
            session.add(Chunk(document_id=doc.id, kb_id=kb.id, owner_id=user.id, seq=seq, content=f"块{seq}"))
        await session.commit()

        await session.delete(doc)
        await session.commit()
        return (
            await session.scalar(select(func.count()).select_from(Chunk)),
            await session.scalar(select(func.count()).select_from(Document)),
        )

    chunk_count, doc_count = run(_check)
    assert (chunk_count, doc_count) == (0, 0)


def test_delete_user_cascades_everything(run):
    """删用户要能级联清掉 token / 知识库 / 文档 / 块，不留孤儿数据"""

    async def _check(session):
        user = await _add_user(session)
        kb = await _add_kb(session, user.id)
        doc = await _add_doc(session, kb.id, user.id)
        session.add(Chunk(document_id=doc.id, kb_id=kb.id, owner_id=user.id, seq=0, content="块0"))
        await session.commit()

        await session.delete(user)
        await session.commit()
        counts = {}
        for model in (KnowledgeBase, Document, Chunk):
            counts[model.__tablename__] = await session.scalar(select(func.count()).select_from(model))
        return counts

    counts = run(_check)
    assert counts == {"knowledge_bases": 0, "documents": 0, "chunks": 0}
