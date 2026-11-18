"""对话接口测试：/api/v1/aichat 的会话管理 + 问答

两个测试替身的取舍得说清：
  - 模型：`answer.get_answer_model()` 是唯一的模型取用口，测试在这里换成假模型，
    于是 SSE 分帧、消息转换、检索、落库这条**真链路**全被跑到
    （直接 monkeypatch 被测函数本身等于没测）。
  - 长期记忆：换成 NullBackend。默认 local 后端会往 data/memory/ 写文件，
    测试结果就会依赖磁盘状态，CI 上跑一次脏一次。
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.db.models import Chunk
from app.rag import answer as answer_service
from app.rag.generation import get_registry, reset_registry
from tests.test_schema_models import _add_doc, _add_kb, _u

BASE = "/api/v1/aichat"


# --------------------------------------------------------------------------
# 夹具与工具
# --------------------------------------------------------------------------


def assert_envelope(resp, status: int):
    body = resp.json()
    assert resp.status_code == status, resp.text
    assert set(body.keys()) == {"status", "msg", "data"}, body
    assert body["status"] == status, body
    return body


@pytest.fixture(autouse=True)
def _clean_registry():
    """注册表是进程级的，用例之间必须清零，否则上一轮的「还在生成」会污染下一轮的 409"""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def _memory_off(monkeypatch):
    from app.rag import chat as chat_service
    from app.rag.memory.base import NullBackend
    from app.rag.memory.store import LongTermMemory

    disabled = LongTermMemory(NullBackend())
    monkeypatch.setattr(chat_service, "get_long_term_memory", lambda: disabled)
    return disabled


class FakeChatModel:
    """流式和一次性都支持的假模型，并留下「模型到底看到了什么」的记录"""

    def __init__(self, pieces=("报销", "流程", "在员工手册里")):
        self.pieces = list(pieces)
        self.seen: list[list] = []

    async def astream(self, messages):
        self.seen.append(list(messages))
        for piece in self.pieces:
            yield SimpleNamespace(content=piece)

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return SimpleNamespace(content="".join(self.pieces))


@pytest.fixture()
def fake_model(monkeypatch):
    model = FakeChatModel()
    monkeypatch.setattr(answer_service, "get_answer_model", lambda: model)
    return model


def make_conversation(client, headers, **body) -> str:
    resp = client.post(f"{BASE}/conversations", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


def history_data(client, headers, cid, **params):
    """取历史消息的 data 体（params 支持 limit / before_seq）"""
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = f"{BASE}/conversations/{cid}/messages" + (f"?{query}" if query else "")
    return assert_envelope(client.get(url, headers=headers), 200)["data"]


def ask(client, headers, cid, question, **extra):
    """发一轮问答。extra 直接进请求体（stream / top_k / auto_title …）"""
    body = {"question": question}
    body.update(extra)
    return client.post(f"{BASE}/conversations/{cid}/chat", json=body, headers=headers)


def prompt_text(model: FakeChatModel) -> str:
    """把最后一次发给模型的东西拼成一整段文本，用来断言资料真的进了 prompt"""
    return " ".join(str(message.content) for message in model.seen[-1])


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """把文本切成 [(event, payload)]，注释帧（: ping）自动跳过"""
    frames = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        assert event is not None and data is not None, block
        frames.append((event, data))
    return frames


def frame_ids(raw: str) -> list[int]:
    ids = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
    return ids


def seed_chunks(
    run, user_id, contents=("员工报销流程：先填单子，再走财务审批。",), name="工作笔记", filename="手册.md"
):
    """建库 + 一篇文档 + 若干块，返回 kb_id"""

    async def _seed(session):
        kb = await _add_kb(session, user_id, name=name)
        doc = await _add_doc(session, kb.id, user_id, filename=filename)
        for index, content in enumerate(contents):
            session.add(
                Chunk(
                    kb_id=_u(kb.id),
                    document_id=_u(doc.id),
                    owner_id=_u(user_id),
                    seq=index,
                    content=content,
                    page=index + 1,
                    heading="报销",
                )
            )
        await session.commit()
        return str(kb.id)

    return run(_seed)


# --------------------------------------------------------------------------
# 1. 会话增删改查
# --------------------------------------------------------------------------


def test_新建会话_用默认标题(client, logged_in):
    body = assert_envelope(client.post(f"{BASE}/conversations", json={}, headers=logged_in["headers"]), 200)
    data = body["data"]
    assert data["title"] == "新对话"
    assert data["kb_id"] is None
    assert data["message_count"] == 0
    assert data["has_summary"] is False
    assert "summary" not in data  # 摘要正文属于内部材料，接口只给长度


def test_新建会话_需要登录(client):
    assert_envelope(client.post(f"{BASE}/conversations", json={}), 401)


def test_新建会话_可以限定知识库(client, logged_in, run):
    kb_id = seed_chunks(run, logged_in["user_id"])
    body = assert_envelope(
        client.post(
            f"{BASE}/conversations",
            json={"kb_id": kb_id, "title": "报销咨询"},
            headers=logged_in["headers"],
        ),
        200,
    )
    assert body["data"]["kb_id"] == kb_id
    assert body["data"]["title"] == "报销咨询"


def test_新建会话_不能圈别人的库(client, logged_in, other_headers, run):
    """越权口径是 404 而不是 403：403 等于向对方承认这个库存在"""
    kb_id = seed_chunks(run, logged_in["user_id"])
    body = assert_envelope(
        client.post(f"{BASE}/conversations", json={"kb_id": kb_id}, headers=other_headers), 404
    )
    assert body["msg"] == "知识库不存在或你没有读取权限"


def test_会话列表_只看到自己的并按最后发言排序(client, logged_in, other_headers, run):
    mine = make_conversation(client, logged_in["headers"], title="我的A")
    make_conversation(client, other_headers, title="别人的")

    async def _touch(session):
        from app.db.crud import chat_crud

        row = await chat_crud.get_session(_u(mine), _u(logged_in["user_id"]), session)
        await chat_crud.append_message(row, "user", "先打个招呼", session)

    run(_touch)

    data = assert_envelope(client.get(f"{BASE}/conversations", headers=logged_in["headers"]), 200)["data"]
    assert data["total"] == 1, "别人的会话绝不能出现在我的列表里"
    assert data["items"][0]["id"] == mine
    assert data["items"][0]["message_count"] == 1
    assert data["items"][0]["last_message_at"] is not None


def test_会话列表_分页与标题搜索(client, logged_in):
    for index in range(3):
        make_conversation(client, logged_in["headers"], title=f"项目{index}讨论")
    make_conversation(client, logged_in["headers"], title="午餐安排")

    page = assert_envelope(
        client.get(f"{BASE}/conversations?page=1&size=2", headers=logged_in["headers"]), 200
    )["data"]
    assert page["total"] == 4 and len(page["items"]) == 2

    found = assert_envelope(
        client.get(f"{BASE}/conversations?keyword=项目", headers=logged_in["headers"]), 200
    )["data"]
    assert found["total"] == 3


def test_会话列表_参数越界由框架拦下(client, logged_in):
    body = assert_envelope(client.get(f"{BASE}/conversations?page=0", headers=logged_in["headers"]), 422)
    assert body["msg"].startswith("参数校验失败")


def test_重命名会话(client, logged_in):
    cid = make_conversation(client, logged_in["headers"])
    body = assert_envelope(
        client.put(f"{BASE}/conversations/{cid}", json={"title": "报销问题"}, headers=logged_in["headers"]), 200
    )
    assert body["data"]["title"] == "报销问题"


def test_重命名_空白标题422_别人的会话404(client, logged_in, other_headers):
    cid = make_conversation(client, logged_in["headers"])
    blank = assert_envelope(
        client.put(f"{BASE}/conversations/{cid}", json={"title": "  "}, headers=logged_in["headers"]), 422
    )
    assert "标题" in blank["msg"] or "title" in blank["msg"]
    assert_envelope(
        client.put(f"{BASE}/conversations/{cid}", json={"title": "抢注"}, headers=other_headers), 404
    )
    assert_envelope(
        client.put(f"{BASE}/conversations/{cid}", json={"title": "改"}, headers=logged_in["headers"]), 200
    )


def test_删除会话_消息一起清掉(client, logged_in, fake_model, run):
    cid = make_conversation(client, logged_in["headers"])
    assert ask(client, logged_in["headers"], cid, "报销流程", stream=False).status_code == 200

    assert_envelope(client.delete(f"{BASE}/conversations/{cid}", headers=logged_in["headers"]), 200)
    assert_envelope(client.delete(f"{BASE}/conversations/{cid}", headers=logged_in["headers"]), 404)

    async def _count(session):
        from sqlalchemy import func, select

        from app.db.models import ChatMessage

        stmt = select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == _u(cid))
        return int((await session.execute(stmt)).scalar_one())

    assert run(_count) == 0, "外键级联没生效（SQLite 忘了开 PRAGMA foreign_keys？）"


def test_删除会话_会取消正在生成的回答(client, logged_in):
    cid = make_conversation(client, logged_in["headers"])
    generation = get_registry().acquire(logged_in["user_id"], cid)
    assert generation is not None
    assert_envelope(client.delete(f"{BASE}/conversations/{cid}", headers=logged_in["headers"]), 200)
    assert generation.is_cancelled is True


# --------------------------------------------------------------------------
# 2. 历史消息
# --------------------------------------------------------------------------


def test_历史消息_按序号升序返回(client, logged_in, fake_model):
    cid = make_conversation(client, logged_in["headers"])
    ask(client, logged_in["headers"], cid, "第一个问题", stream=False)
    ask(client, logged_in["headers"], cid, "第二个问题", stream=False)

    data = history_data(client, logged_in["headers"], cid)
    assert [item["seq"] for item in data["items"]] == [1, 2, 3, 4]
    assert [item["role"] for item in data["items"]] == ["user", "assistant", "user", "assistant"]
    assert data["total"] == 4 and data["has_more"] is False
    assert data["session"]["id"] == cid
    assert data["items"][1]["content"] == "报销流程在员工手册里"


def test_历史消息_游标翻页(client, logged_in, fake_model):
    cid = make_conversation(client, logged_in["headers"])
    ask(client, logged_in["headers"], cid, "问题一", stream=False)
    ask(client, logged_in["headers"], cid, "问题二", stream=False)

    latest = history_data(client, logged_in["headers"], cid, limit=2)
    assert [item["seq"] for item in latest["items"]] == [3, 4]
    assert latest["has_more"] is True

    older = history_data(client, logged_in["headers"], cid, limit=2, before_seq=latest["items"][0]["seq"])
    assert [item["seq"] for item in older["items"]] == [1, 2]
    assert older["has_more"] is False


def test_历史消息_越权与非法id(client, logged_in, other_headers):
    cid = make_conversation(client, logged_in["headers"])
    assert_envelope(client.get(f"{BASE}/conversations/{cid}/messages", headers=other_headers), 404)
    assert_envelope(client.get(f"{BASE}/conversations/not-uuid/messages", headers=logged_in["headers"]), 422)


# --------------------------------------------------------------------------
# 3. 问答：非流式车道
# --------------------------------------------------------------------------


def test_问答_落库两条消息并累计计数(client, logged_in, fake_model):
    cid = make_conversation(client, logged_in["headers"])
    body = assert_envelope(ask(client, logged_in["headers"], cid, "报销流程是什么", stream=False), 200)
    data = body["data"]

    assert data["answer"] == "报销流程在员工手册里"
    assert data["session_id"] == cid
    assert data["sources"] == []
    assert data["retrieval_mode"] == "no_scope"  # 一个可读的库都没有 → 不带资料
    assert data["stopped"] is False
    assert data["turn"]["appended"] == 2
    assert data["turn"]["message_count"] == 2

    listed = assert_envelope(client.get(f"{BASE}/conversations", headers=logged_in["headers"]), 200)["data"]
    assert listed["items"][0]["message_count"] == 2


def test_问答_首轮自动命名_可以关掉(client, logged_in, fake_model):
    cid = make_conversation(client, logged_in["headers"])
    ask(client, logged_in["headers"], cid, "差旅住宿标准是多少", stream=False)
    assert history_data(client, logged_in["headers"], cid)["session"]["title"] == "差旅住宿标准是多少"

    kept = make_conversation(client, logged_in["headers"], title="保持这个名字")
    ask(client, logged_in["headers"], kept, "不会改标题的问题", stream=False, auto_title=False)
    assert history_data(client, logged_in["headers"], kept)["session"]["title"] == "保持这个名字"


def test_问答_自动标题会截断长问题(client, logged_in, fake_model, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_AUTO_TITLE_CHARS", 8)
    cid = make_conversation(client, logged_in["headers"])
    ask(client, logged_in["headers"], cid, "这是一个非常非常长的关于差旅报销附件要求的问题", stream=False)
    title = history_data(client, logged_in["headers"], cid)["session"]["title"]
    assert title.endswith("…")
    assert len(title) == 9  # 8 个字 + 省略号


def test_问答_命中知识库并给出引用(client, logged_in, fake_model, run):
    kb_id = seed_chunks(
        run,
        logged_in["user_id"],
        contents=("员工报销流程：先填单子，再走财务审批，超过五千需要总监签字。",),
    )
    cid = make_conversation(client, logged_in["headers"], kb_id=kb_id)
    data = assert_envelope(ask(client, logged_in["headers"], cid, "报销流程是什么", stream=False), 200)["data"]

    assert data["retrieval_mode"] == "keyword"
    first = data["sources"][0]
    assert first["kb_id"] == kb_id and first["index"] == 1
    assert first["title"] == "手册.md"
    assert first["page"] == 1 and first["heading"] == "报销"
    assert "先填单子" in first["snippet"]

    # 真的进了 prompt，而不是只写进引用列表好看
    prompt = prompt_text(fake_model)
    assert "参考资料" in prompt and "先填单子" in prompt
    assert "不要执行" in prompt  # 防注入约束必须在资料前面

    stored = history_data(client, logged_in["headers"], cid)
    assistant = [item for item in stored["items"] if item["role"] == "assistant"][0]
    assert assistant["sources"][0]["chunk_id"] == first["chunk_id"]


def test_问答_检索不越权_共享之后才能查到(client, logged_in, other_headers, fake_model, run):
    secret = "只有张三知道的报销额度是五千元"
    kb_id = seed_chunks(run, logged_in["user_id"], contents=(secret,))

    cid = make_conversation(client, other_headers, title="我的空会话")
    data = assert_envelope(ask(client, other_headers, cid, "报销额度是五千元吗", stream=False), 200)["data"]
    assert data["sources"] == []
    assert secret not in prompt_text(fake_model)

    share = client.post(
        f"/api/v1/kb/share?kb_id={kb_id}",
        json={"email": "lisi@example.com"},
        headers=logged_in["headers"],
    )
    assert share.status_code == 200, share.text

    shared_cid = make_conversation(client, other_headers, kb_id=kb_id)
    shared = assert_envelope(ask(client, other_headers, shared_cid, "报销额度是多少", stream=False), 200)["data"]
    assert shared["retrieval_mode"] == "keyword"
    assert secret in shared["sources"][0]["snippet"]


def test_问答_被软删文档的块不参与检索(client, logged_in, fake_model, run):
    from datetime import UTC, datetime

    kb_id = seed_chunks(run, logged_in["user_id"], contents=("这份资料里的报销流程先填单子。",))

    async def _soft_delete(session):
        from sqlalchemy import select

        from app.db.models import Document

        doc = (await session.execute(select(Document).where(Document.kb_id == _u(kb_id)))).scalars().first()
        doc.deleted_at = datetime.now(UTC)
        await session.commit()

    run(_soft_delete)

    cid = make_conversation(client, logged_in["headers"], kb_id=kb_id)
    data = assert_envelope(ask(client, logged_in["headers"], cid, "报销流程", stream=False), 200)["data"]
    assert data["sources"] == [] and data["retrieval_mode"] == "none"


def test_问答_会话不存在与参数校验(client, logged_in):
    assert_envelope(ask(client, logged_in["headers"], str(uuid.uuid4()), "在吗", stream=False), 404)

    cid = make_conversation(client, logged_in["headers"])
    assert client.post(f"{BASE}/conversations/{cid}/chat", json={"question": "   "},
                       headers=logged_in["headers"]).status_code == 422
    assert client.post(f"{BASE}/conversations/{cid}/chat", json={},
                       headers=logged_in["headers"]).status_code == 422
    assert client.post(f"{BASE}/conversations/{cid}/chat", json={"question": "x" * 7000},
                       headers=logged_in["headers"]).status_code == 422


def test_问答_模型不可用时给503而不是半条流(client, logged_in, monkeypatch):
    from app.rag.memory.base import MemoryNotConfiguredError

    def boom():
        raise MemoryNotConfiguredError("未配置 LLM_API_KEY")

    monkeypatch.setattr(answer_service, "get_answer_model", boom)
    cid = make_conversation(client, logged_in["headers"])
    body = assert_envelope(ask(client, logged_in["headers"], cid, "报销流程", stream=False), 503)
    assert "不可用" in body["msg"] or "未配置大模型" in body["msg"]
    # 关键：会话没被写脏，模型不可用时不落半条消息
    assert history_data(client, logged_in["headers"], cid)["items"] == []
    assert get_registry().count() == 0


def test_问答_模型调用失败时非流式车道报503并带trace(client, logged_in, monkeypatch):
    class BrokenModel:
        async def ainvoke(self, messages):
            raise RuntimeError("upstream 500 with secret api_key=sk-should-not-leak")

    monkeypatch.setattr(answer_service, "get_answer_model", lambda: BrokenModel())
    cid = make_conversation(client, logged_in["headers"])
    body = assert_envelope(ask(client, logged_in["headers"], cid, "报销流程", stream=False), 503)
    assert "secret" not in body["msg"]
    assert "sk-" not in json.dumps(body, ensure_ascii=False), "异常原文不能漏进响应"
    assert body["data"]["trace_id"]


# --------------------------------------------------------------------------
# 4. 问答：流式车道
# --------------------------------------------------------------------------


def test_流式问答_帧序列与响应头(client, logged_in, fake_model, run):
    kb_id = seed_chunks(run, logged_in["user_id"], contents=("报销流程：先填单子再走财务审批。",))
    cid = make_conversation(client, logged_in["headers"], kb_id=kb_id)

    resp = ask(client, logged_in["headers"], cid, "报销流程")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"  # 漏了这头 nginx 会把答案攒完才发

    frames = parse_sse(resp.text)
    names = [event for event, _ in frames]
    assert names[0] == "meta" and names[-1] == "done"
    assert names.count("references") == 1 and names.index("references") < names.index("done")
    assert names.count("token") == 3

    token_text = "".join(payload["data"]["text"] for event, payload in frames if event == "token")
    assert token_text == "报销流程在员工手册里"
    assert frames[-1][1]["data"]["text"] == token_text  # done 带全文，前端直接落定气泡

    for _event, payload in frames:  # 每帧载荷仍是 {status,msg,data}
        assert set(payload.keys()) == {"status", "msg", "data"}, payload
        assert payload["status"] == 200
    assert frame_ids(resp.text) == list(range(1, len(frames) + 1)), "id 必须递增，断线续传靠它"

    assert frames[names.index("references")][1]["data"]["items"][0]["kb_id"] == kb_id
    meta = frames[0][1]["data"]
    assert meta["session_id"] == cid and meta["retrieval"]["mode"] == "keyword"

    # 流结束后确实落库了（用的是新开的 Session，不是请求级那个）
    stored = history_data(client, logged_in["headers"], cid)
    assert [item["role"] for item in stored["items"]] == ["user", "assistant"]
    assert stored["items"][1]["sources"][0]["kb_id"] == kb_id


def test_流式问答_会话不存在时仍是普通404(client, logged_in, fake_model):
    resp = ask(client, logged_in["headers"], str(uuid.uuid4()), "在吗")
    assert resp.status_code == 404
    assert not resp.headers["content-type"].startswith("text/event-stream")


def test_流式问答_同会话第二次发送被409挡住(client, logged_in, fake_model):
    cid = make_conversation(client, logged_in["headers"])
    assert get_registry().acquire(logged_in["user_id"], cid) is not None
    body = assert_envelope(ask(client, logged_in["headers"], cid, "重复发送"), 409)
    assert "正在生成" in body["msg"]

    other = make_conversation(client, logged_in["headers"])  # 别的会话不受影响
    assert ask(client, logged_in["headers"], other, "hi").status_code == 200


def test_流式问答_僵尸键可以被顶掉(client, logged_in, monkeypatch):
    from app.core.config import settings

    cid = make_conversation(client, logged_in["headers"])
    first = get_registry().acquire(logged_in["user_id"], cid)
    assert first is not None
    assert get_registry().acquire(logged_in["user_id"], cid) is None

    monkeypatch.setattr(settings, "CHAT_STALE_GENERATION_SECONDS", -1)
    second = get_registry().acquire(logged_in["user_id"], cid)
    assert second is not None and second.generation_id != first.generation_id
    assert first.is_cancelled is True, "旧的必须被取消，否则它还在往已废弃的流里吐字"


class StoppingChatModel:
    """生成到一半时「用户点了停止」：注册表被置位，而模型还在往下产出"""

    def __init__(self, user_id, conversation_id):
        self.user_id = user_id
        self.conversation_id = conversation_id

    async def astream(self, messages):
        yield SimpleNamespace(content="已经生成的半截答案")
        get_registry().cancel(self.user_id, self.conversation_id)
        yield SimpleNamespace(content="这段不该被发出去")

    async def ainvoke(self, messages):  # pragma: no cover —— 流式车道不走这里
        return SimpleNamespace(content="")


def test_停止_置标志后流正常收尾并保留半截答案(client, logged_in, monkeypatch):
    cid = make_conversation(client, logged_in["headers"])
    monkeypatch.setattr(answer_service, "get_answer_model", lambda: StoppingChatModel(logged_in["user_id"], cid))

    resp = ask(client, logged_in["headers"], cid, "长答案")
    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    names = [event for event, _ in frames]
    assert names[-1] == "done" and "error" not in names
    assert [payload["data"]["text"] for event, payload in frames if event == "token"] == ["已经生成的半截答案"]

    items = history_data(client, logged_in["headers"], cid)["items"]
    assert [item["content"] for item in items] == ["长答案", "已经生成的半截答案"]
    assert get_registry().count() == 0, "生成结束要注销，否则这个会话永远发不出下一条"


def test_停止接口_没有在生成时也算成功(client, logged_in):
    cid = make_conversation(client, logged_in["headers"])
    body = assert_envelope(client.post(f"{BASE}/conversations/{cid}/stop", headers=logged_in["headers"]), 200)
    assert body["data"] == {"session_id": cid, "stopped": False, "generation_id": None, "partial_chars": 0}
    assert "没有正在生成" in body["msg"]


def test_停止接口_真的取消在跑的那一路并统计字数(client, logged_in):
    cid = make_conversation(client, logged_in["headers"])
    generation = get_registry().acquire(logged_in["user_id"], cid)
    generation.add_chars(42)

    body = assert_envelope(client.post(f"{BASE}/conversations/{cid}/stop", headers=logged_in["headers"]), 200)
    assert body["data"]["stopped"] is True
    assert body["data"]["generation_id"] == generation.generation_id
    assert body["data"]["partial_chars"] == 42
    assert generation.is_cancelled is True
    # 重复点击不报错（手机弱网下前端一定会重发）
    assert assert_envelope(
        client.post(f"{BASE}/conversations/{cid}/stop", headers=logged_in["headers"]), 200
    )["data"]["stopped"] is True


def test_停止接口_越权404(client, logged_in, other_headers):
    cid = make_conversation(client, logged_in["headers"])
    get_registry().acquire(logged_in["user_id"], cid)
    assert_envelope(client.post(f"{BASE}/conversations/{cid}/stop", headers=other_headers), 404)
    assert get_registry().get(logged_in["user_id"], cid).is_cancelled is False, "别人的 stop 不该生效"


# --------------------------------------------------------------------------
# 5. Agent 目录
# --------------------------------------------------------------------------


def test_agents列表_带可用性说明(client, logged_in):
    from app.core.config import settings

    items = assert_envelope(client.get(f"{BASE}/agents", headers=logged_in["headers"]), 200)["data"]
    assert [item["key"] for item in items] == ["rag_default", "rag_kb_only", "agent_tools"]
    for item in items[:-1]:
        assert item["available"] is settings.llm_ready
        assert (item["unavailable_reason"] is None) is settings.llm_ready
    assert items[1]["needs_kb"] is True
    assert items[-1]["available"] is False and "M8" in items[-1]["unavailable_reason"]


def test_agents列表也要登录(client):
    assert_envelope(client.get(f"{BASE}/agents"), 401)
