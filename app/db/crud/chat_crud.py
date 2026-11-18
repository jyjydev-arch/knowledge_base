"""chat_sessions / chat_messages 的数据访问

规矩和别的 crud 一样：只读写库、不抛 HTTP 异常、查不到返回 None。

**每个函数都必须带 user_id**。会话内容是最容易越权的地方（拿到别人的 session_id
就能读完整段对话，甚至看到别人知识库里的资料片段），所以这里没有"只按 session_id 查"的口子。
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessage, ChatRole, ChatSession
from app.services.text_metrics import estimate_tokens


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def _attach(session: ChatSession, db: AsyncSession) -> ChatSession | None:
    """把可能游离的会话对象接回当前 Session

    为什么需要：服务层之间（以及测试里跨多次 asyncio.run）很容易把一个从别的 Session
    取出来的对象传进来，直接改它再 commit 会 InvalidRequestError。
    注意 AsyncSession **没有** .contains()（那是同步 Session 的方法），
    用 db.get() 重新取：命中同一 Session 的 identity map 时不会有额外查询，行没了则返回 None。
    """
    if isinstance(session, ChatSession) and session.id is None:
        return None
    return await db.get(ChatSession, session.id)


async def create_session(
    user_id, db: AsyncSession, *, title: str | None = None, kb_id=None
) -> ChatSession | None:
    """新建会话。kb_id 传了就得是合法 uuid，否则当没传（范围可以之后改）"""
    uid = _to_uuid(user_id)
    if uid is None:
        return None
    record = ChatSession(
        user_id=uid,
        kb_id=_to_uuid(kb_id),
        title=(title or "新对话")[:255],
    )
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:  # kb_id 指向不存在的库（外键失败）
        await db.rollback()
        return None
    await db.refresh(record)
    return record


async def get_session(session_id, user_id, db: AsyncSession) -> ChatSession | None:
    """按 (id, user_id) 双条件取会话 —— 越权访问天然返回 None"""
    sid, uid = _to_uuid(session_id), _to_uuid(user_id)
    if sid is None or uid is None:
        return None
    result = await db.execute(select(ChatSession).where(ChatSession.id == sid, ChatSession.user_id == uid))
    return result.scalar_one_or_none()


async def list_sessions(
    user_id, db: AsyncSession, offset: int = 0, limit: int = 20, keyword: str | None = None
) -> tuple[list[ChatSession], int]:
    """会话列表：按最后一次发言时间倒序（不是创建时间 —— 用户找的是"刚才聊的那个"）"""
    uid = _to_uuid(user_id)
    if uid is None:
        return [], 0
    conditions = [ChatSession.user_id == uid]
    if keyword:
        conditions.append(ChatSession.title.ilike(f"%{keyword.strip()}%"))
    total = int(
        (await db.execute(select(func.count()).select_from(ChatSession).where(*conditions))).scalar_one()
    )
    result = await db.execute(
        select(ChatSession)
        .where(*conditions)
        .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def rename_session(session: ChatSession, title: str, db: AsyncSession) -> ChatSession:
    attached = await _attach(session, db)
    if attached is None:
        return None
    session = attached
    session.title = (title or session.title)[:255]
    await db.commit()
    await db.refresh(session)
    return session


async def save_summary(
    session: ChatSession, summary: str, db: AsyncSession, upto_seq: int | None = None
) -> ChatSession:
    """存"早期对话压缩后的摘要" + 水位线

    upto_seq = 这条摘要已经覆盖到第几号消息；留空表示只改文本、不动水位线。
    摘要清空传空串而不是 None（列是 NOT NULL）。
    """
    attached = await _attach(session, db)
    if attached is None:
        return None
    session = attached
    session.summary = summary or ""
    if upto_seq is not None:
        session.summary_seq = max(int(session.summary_seq or 0), int(upto_seq))
    await db.commit()
    await db.refresh(session)
    return session


async def list_messages_after(session: ChatSession, db: AsyncSession, *, limit: int = 40) -> list[ChatMessage]:
    """取水位线之后的消息（按 seq 升序）—— 压缩只需要这部分，不该整段重读"""
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id, ChatMessage.seq > int(session.summary_seq or 0))
        .order_by(ChatMessage.seq)
        .limit(limit)
    )
    return list(result.scalars().all())


async def delete_session(session: ChatSession, db: AsyncSession) -> bool:
    """删会话。消息由外键级联清掉（关系上 passive_deletes=True，不会把几百条先查出来）"""
    await db.delete(session)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def next_seq(session_id, db: AsyncSession) -> int:
    sid = _to_uuid(session_id)
    current = int(
        (
            await db.execute(select(func.coalesce(func.max(ChatMessage.seq), 0)).where(ChatMessage.session_id == sid))
        ).scalar_one()
    )
    return current + 1


async def append_message(
    session: ChatSession,
    role: str,
    content: str,
    db: AsyncSession,
    *,
    sources: list[dict] | None = None,
) -> ChatMessage | None:
    """追加一条消息，并同步会话计数与 last_message_at

    seq 由 max(seq)+1 得来，配合 uq(session_id, seq)：并发写同名序号会撞唯一索引而不是
    悄悄多出两条重复消息。撞了就重算一次（比让接口 500 好，也比静默重复好）。
    """
    text = (content or "").strip()
    if not text:
        return None
    # 会话对象可能是从别的 Session 带来的，先接回来（详见 _attach 的说明）
    attached = await _attach(session, db)
    if attached is None:
        return None
    session = attached
    role_value = str(role or ChatRole.USER).strip().lower()
    if role_value not in set(ChatRole):
        role_value = ChatRole.USER

    for _attempt in range(2):
        record = ChatMessage(
            session_id=session.id,
            user_id=session.user_id,
            role=role_value,
            content=text,
            token_count=estimate_tokens(text),
            seq=await next_seq(session.id, db),
            sources=list(sources or []),
        )
        db.add(record)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            continue
        await db.refresh(record)
        await _bump_session_counts(session, record, db)
        return record
    return None


async def _bump_session_counts(session: ChatSession, message: ChatMessage, db: AsyncSession) -> None:
    from datetime import datetime

    session.message_count = int(session.message_count or 0) + 1
    session.token_count = int(session.token_count or 0) + int(message.token_count or 0)
    session.last_message_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(session)


async def list_messages(
    session_id,
    user_id,
    db: AsyncSession,
    *,
    limit: int = 50,
    before_seq: int | None = None,
) -> list[ChatMessage]:
    """按 seq 升序返回，`before_seq` 用于"向上翻页加载更早的消息"

    移动端必须分页：一次拉整段历史既慢又爆内存，而且首屏只需要最近几轮。
    """
    sid, uid = _to_uuid(session_id), _to_uuid(user_id)
    if sid is None or uid is None:
        return []
    conditions = [ChatMessage.session_id == sid, ChatMessage.user_id == uid]
    if before_seq is not None:
        conditions.append(ChatMessage.seq < int(before_seq))
    result = await db.execute(
        select(ChatMessage).where(*conditions).order_by(ChatMessage.seq.desc()).limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def recent_messages(session_id, user_id, db: AsyncSession, limit: int = 12) -> list[ChatMessage]:
    """短期窗口取数：最近 limit 条，按时间正序返回"""
    rows = await list_messages(session_id, user_id, db, limit=limit)
    return rows


async def count_messages(session_id, user_id, db: AsyncSession) -> int:
    sid, uid = _to_uuid(session_id), _to_uuid(user_id)
    if sid is None or uid is None:
        return 0
    return int(
        (
            await db.execute(
                select(func.count()).select_from(ChatMessage).where(
                    ChatMessage.session_id == sid, ChatMessage.user_id == uid
                )
            )
        ).scalar_one()
    )


async def delete_sessions_of_kb(kb_id, db: AsyncSession) -> int:
    """删库时用：把该库的会话标题里的范围引用清掉（kb_id 本身是 SET NULL，会话不删）"""
    pk = _to_uuid(kb_id)
    if pk is None:
        return 0
    result = await db.execute(update(ChatSession).where(ChatSession.kb_id == pk).values(kb_id=None))
    await db.commit()
    return int(result.rowcount or 0)


async def delete_all_sessions(user_id, db: AsyncSession) -> int:
    """注销账号时用。消息由外键级联清掉"""
    uid = _to_uuid(user_id)
    if uid is None:
        return 0
    result = await db.execute(delete(ChatSession).where(ChatSession.user_id == uid))
    await db.commit()
    return int(result.rowcount or 0)


__all__ = [
    "append_message",
    "count_messages",
    "create_session",
    "delete_all_sessions",
    "delete_session",
    "delete_sessions_of_kb",
    "get_session",
    "list_messages",
    "list_messages_after",
    "list_sessions",
    "next_seq",
    "recent_messages",
    "rename_session",
    "save_summary",
]
