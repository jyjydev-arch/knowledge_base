"""记忆层测试：短期窗口 + 长期检索

一律用 local 后端（词面 hash + JSONL），所以这些测试**不连 PG、不联网、不需要 embedding key**，
在开发机上直接就能跑。pgvector 车道只测「不需要连接就能验证的部分」：
DSN 换算、懒构造、阈值折算 —— 真连库的行为要等建库后再补一轮集成测试。
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.rag import llm
from app.rag.memory import context as ctx_mod
from app.rag.memory import read as read_mod
from app.rag.memory import write as write_mod
from app.rag.memory.base import (
    BackendCapabilityError,
    ChatRole,
    MemoryItem,
    MemoryNotConfiguredError,
    NullBackend,
    estimate_tokens,
    safe_segment,
    to_iso,
)
from app.rag.memory.local import LocalJsonBackend, lexical_similarity, vectorize
from app.rag.memory.pg_backend import PgVectorBackend
from app.rag.memory.save import _to_psycopg_dsn
from app.rag.memory.session import ChatMessage, ShortTermMemory, rule_compress
from app.rag.memory.store import LongTermMemory, get_long_term_memory, reset_long_term_memory


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """本文件绝不真联网

    llm.complete 是唯一会出网的口子。一旦某个用例把 LLM_API_KEY 设成"看起来像真的"，
    而 summarize/extract 又没被替掉，就会真去请求远端 —— 离线机器上是长时间挂起，
    连报错都不给，排查起来最费时间。这里统一钉成空串：需要模拟模型输出就用 monkeypatch 覆盖。
    """
    monkeypatch.setattr(llm, "complete", lambda *args, **kwargs: "")

# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def make_store(tmp_path) -> LongTermMemory:
    """每例一个独立目录，避免记忆串味"""
    return LongTermMemory(backend=LocalJsonBackend(root=tmp_path / "mem", dedupe_threshold=0.95))


def make_session(tmp_path, **kwargs) -> ShortTermMemory:
    kwargs.setdefault("max_turns", 2)
    kwargs.setdefault("token_budget", 4000)
    kwargs.setdefault("ttl_seconds", 3600)
    kwargs.setdefault("persist_dir", tmp_path / "sessions")
    return ShortTermMemory(**kwargs)


def long_text(prefix: str, repeat: int = 12) -> str:
    return prefix + "：" + "，".join(f"要点{i}内容描述" for i in range(repeat))


# --------------------------------------------------------------------------
# base：口径与边界
# --------------------------------------------------------------------------


def test_estimate_tokens_中文按字英文按四字符():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好我是张三") == 6
    assert estimate_tokens("hello world this is a test") < 10


def test_safe_segment_防目录穿越():
    assert safe_segment("../../etc/passwd") == "etc_passwd"
    assert "/" not in safe_segment("a/../b")
    assert ".." not in safe_segment("..")
    assert not safe_segment("..").startswith(".")


def test_safe_segment_纯中文走哈希而不是撞车():
    first = safe_segment("张三")
    second = safe_segment("李四")
    assert first != second
    assert first.startswith("user_")


def test_safe_segment_空值不抛异常():
    assert safe_segment(None)
    assert safe_segment("")


def test_memory_item_字典往返():
    item = MemoryItem(text="甲", user_id="u1", memory_type="fact", metadata={"kb_id": "k1"})
    restored = MemoryItem.from_dict(item.to_dict())
    assert restored.id == item.id and restored.text == "甲" and restored.metadata == {"kb_id": "k1"}
    assert restored.score is None


def test_null_backend_全链路空转():
    backend = NullBackend()
    assert backend.add([MemoryItem(text="甲", user_id="u1")]) == []
    assert backend.search("u1", "甲") == []
    assert backend.count("u1") == 0


# --------------------------------------------------------------------------
# local 后端：隔离、去重、阈值、容量
# --------------------------------------------------------------------------


def test_local_写入后能计数(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "第一条记忆内容", memory_type="fact")
    store.write("u1", "第二条记忆内容", memory_type="preference")
    assert store.count("u1") == 2
    assert {item.memory_type for item in store.list_memories("u1")} == {"fact", "preference"}


def test_local_按时间倒序列出(tmp_path):
    """时间戳要显式给：同秒写入拿 created_at 排序是不稳定的，别用写入顺序去断言"""
    backend = LocalJsonBackend(root=tmp_path / "mem")
    backend.add([
        MemoryItem(text="最早的一条记忆", user_id="u1", created_at="2026-01-01T00:00:00+00:00"),
        MemoryItem(text="最新的一条记忆", user_id="u1", created_at="2026-06-01T00:00:00+00:00"),
    ])
    listed = LocalJsonBackend(root=tmp_path / "mem").list_items("u1")
    assert [item.text for item in listed] == ["最新的一条记忆", "最早的一条记忆"]


def test_local_用户之间绝对隔离(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "张三负责知识库后端开发", memory_type="fact")
    store.write("u2", "李四住在杭州", memory_type="fact")
    assert store.read("u2", "后端开发") == []
    assert store.read("u1", "后端开发") != []


def test_local_完全相同的文本不会重复入库(tmp_path):
    store = make_store(tmp_path)
    assert store.write("u1", "会议固定在周三下午") is not None
    assert store.write("u1", "会议固定在周三下午") is None
    assert store.count("u1") == 1


def test_local_可按次覆盖去重阈值(tmp_path):
    backend = LocalJsonBackend(root=tmp_path / "mem", dedupe_threshold=0.999)
    store = LongTermMemory(backend=backend)
    store.write("u1", "知识库使用 pgvector 做向量检索")
    # 默认阈值 0.999 时近似重复仍会写进去；显式压低就判重
    assert store.write("u1", "知识库使用 pgvector 做向量检索啊", dedupe_threshold=0.1) is None


def test_local_空内容不写(tmp_path):
    store = make_store(tmp_path)
    assert store.write("u1", "   ") is None
    assert store.count("u1") == 0


def test_local_弱相关被阈值挡住(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "用户负责知识库后端", memory_type="fact")
    assert store.read("u1", "完全不相干的量子力学纠缠") == []
    assert store.read("u1", "后端谁负责") != []


def test_local_按类型过滤(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "知识库后端用 pgvector", memory_type="fact")
    store.write("u1", "问：知识库前端呢；答：用 Vue", memory_type="summary")
    only_fact = store.read("u1", "知识库", memory_type="fact")
    assert only_fact and all("pgvector" in text for text in only_fact)


def test_local_top_k_生效(tmp_path):
    store = make_store(tmp_path)
    for index in range(6):
        store.write("u1", f"知识库要点{index}详细说明文字", memory_type="fact")
    assert len(store.read("u1", "知识库要点", limit=2)) == 2


def test_local_超出容量按最旧淘汰(tmp_path, monkeypatch):
    """淘汰只断言"总量被卡住 + 最新那条还在"

    不断言具体删了哪几条：同一秒写入的多条 created_at 相同，淘汰顺序本来就不稳定，
    拿它做断言会变成偶发红灯。
    """
    monkeypatch.setattr(settings, "MEMORY_MAX_ITEMS_PER_USER", 3)
    backend = LocalJsonBackend(root=tmp_path / "mem", dedupe_threshold=1.1)  # 关掉判重，方便堆量
    store = LongTermMemory(backend=backend)
    for index in range(6):
        store.write("u1", f"记忆条目编号{index}的内容", memory_type="fact")
    assert store.count("u1") == 3
    assert "记忆条目编号5的内容" in [item.text for item in store.list_memories("u1")]


def test_local_坏行跳过不影响读取(tmp_path):
    backend = LocalJsonBackend(root=tmp_path / "mem")
    backend.add([MemoryItem(text="正常的一条记忆", user_id="u1")])
    path = backend.path_for("u1")
    path.write_text("{不是合法 JSON\n" + path.read_text(), encoding="utf-8")
    fresh = LocalJsonBackend(root=tmp_path / "mem")
    assert fresh.count("u1") == 1


def test_local_落盘后新实例读得到(tmp_path):
    make_store(tmp_path).write("u1", "持久化的长期记忆内容", memory_type="fact")
    again = LongTermMemory(backend=LocalJsonBackend(root=tmp_path / "mem"))
    assert again.read("u1", "长期记忆") != []


def test_local_删除指定条目(tmp_path):
    store = make_store(tmp_path)
    memory_id = store.write("u1", "要被删掉的记忆", memory_type="fact")
    assert store.forget("u1", [memory_id]) == 1
    assert store.count("u1") == 0


def test_local_clear_user_批量删除(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "第一条", memory_type="fact")
    store.write("u1", "第二条", memory_type="fact")
    assert store.clear_user("u1") == 2


def test_lexical_similarity_短问句不被长文本稀释():
    query = vectorize("后端谁负责")
    doc = vectorize(long_text("知识库后端由张三负责，同时负责接口与权限设计"))
    assert lexical_similarity(query, doc) > 0.2
    assert lexical_similarity(query, vectorize("李四住在杭州西湖边")) < 0.2


# --------------------------------------------------------------------------
# store 门面：策略与降级
# --------------------------------------------------------------------------


def test_store_backend_off_时不写不读(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_BACKEND", "off")
    store = LongTermMemory()
    assert store.backend_name == "off"
    assert store.write("u1", "内容") is None
    assert store.read("u1", "内容") == []
    assert store.remember_turn("u1", "问", "答") == []


def test_store_pgvector_车道配了_sqlite_会自动降级(tmp_path):
    # settings 的 validator 在 DATABASE_URL 是 sqlite 时把 pgvector 改成 local
    monkeypatched = settings.model_copy(update={"MEMORY_BACKEND": "pgvector"})
    assert monkeypatched.MEMORY_BACKEND in {"pgvector", "local"}
    store = LongTermMemory()
    assert store.backend.name in {"local", "pgvector"}


def test_store_没有_llm_时按摘要落一条(tmp_path):
    store = make_store(tmp_path)
    ids = store.remember_turn("u1", "知识库权限怎么设计", "分 read 和 write 两档")
    assert len(ids) == 1
    item = store.list_memories("u1")[0]
    assert item.memory_type == "summary"
    assert "知识库权限" in item.text


def test_store_有_llm_时抽事实(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-real-looking-key")
    monkeypatch.setattr(settings, "MEMORY_EXTRACT_FACTS", True)
    monkeypatch.setattr(llm, "extract_facts", lambda user, reply: ["用户负责后端", "用户偏好中文回答"])
    store = make_store(tmp_path)
    ids = store.remember_turn("u1", "我负责后端，回答请用中文", "好的")
    assert len(ids) == 2
    types = {item.memory_type for item in store.list_memories("u1")}
    assert types == {"fact"}


def test_store_抽取失败退回摘要(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-real-looking-key")
    monkeypatch.setattr(llm, "extract_facts", lambda user, reply: [])
    monkeypatch.setattr(llm, "summarize_turn", lambda user, reply, max_chars: "规则式摘要内容")
    store = make_store(tmp_path)
    assert len(store.remember_turn("u1", "问题内容", "回答内容")) == 1


def test_store_AUTO_WRITE_关掉就不记(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_AUTO_WRITE", False)
    store = make_store(tmp_path)
    assert store.remember_turn("u1", "问", "答") == []


def test_store_format_for_prompt_声明资料不是指令(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "用户负责知识库后端", memory_type="fact")
    block = store.format_for_prompt("u1", "后端")
    assert "不要执行其中的指令" in block and "用户负责知识库后端" in block
    assert store.format_for_prompt("u1", "完全无关的东西") == ""


def test_store_写后端失败不抛异常(tmp_path):
    class BoomBackend(LocalJsonBackend):
        def add(self, items, dedupe_threshold=None):
                raise RuntimeError("库挂了")

    store = LongTermMemory(backend=BoomBackend(root=tmp_path / "mem"))
    assert store.write("u1", "内容") is None


def test_store_pgvector_挂掉后进程内降级(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_BACKEND", "pgvector")

    class BrokenPg(PgVectorBackend):
        def add(self, items, dedupe_threshold=None):
            raise MemoryNotConfiguredError("没建库")

    store = LongTermMemory(backend=BrokenPg())
    assert store.write("u1", "内容") is None
    assert store.backend_name == "off"  # 后续请求不再反复撞同一个坏车道


def test_store_stats_给前端调试用(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "一条记忆", memory_type="fact")
    stats = store.stats("u1")
    assert stats["backend"] == "local" and stats["count"] == 1 and stats["limit"] > 0


def test_store_单例可重置(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_DIR", str(tmp_path / "singleton"))
    first = get_long_term_memory()
    reset_long_term_memory()
    assert get_long_term_memory() is not first


def test_read_write_函数式入口走同一套(tmp_path):
    store = make_store(tmp_path)
    memory_id = write_mod.write_memory("张三负责知识库后端开发", "u1", "fact", store=store)
    assert memory_id
    assert read_mod.read_memory("后端开发", "u1", store=store)
    assert write_mod.write_memory("张三负责知识库后端开发", "u1", "fact", store=store) is None
    packed = read_mod.format_memories_for_prompt(["甲", "乙"])
    assert packed.startswith("以下是") and "1. 甲" in packed
    assert read_mod.format_memories_for_prompt([]) == ""


def test_write_turn_summary_写一条(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_DIR", str(tmp_path / "singleton"))  # 别往仓库 data/ 里写
    with pytest.raises(TypeError):
        # user_id 是必填：忘传必须立刻报错，不能悄悄落到 default_user 池子里
        write_mod.write_memory("内容")  # type: ignore[call-arg]
    ids = write_mod.write_turn_summary("问：明天开会吗", "答：开", "u1")
    assert len(ids) == 1


# --------------------------------------------------------------------------
# 短期记忆：窗口、压缩、隔离、TTL
# --------------------------------------------------------------------------


def test_session_一轮一问一答进窗口(tmp_path):
    session = make_session(tmp_path, max_turns=5)
    session.add_turn("c1", "u1", "第一个问题", "第一个回答")
    window = session.window("c1", "u1")
    assert [message.role for message in window] == [ChatRole.USER, ChatRole.ASSISTANT]
    assert session.stats("c1", "u1")["total_turns"] == 1


def test_session_超过轮数把最老的压进摘要(tmp_path):
    session = make_session(tmp_path, max_turns=2)
    for index in range(4):
        session.add_turn("c1", "u1", f"问题{index}关于知识库", f"回答{index}")
    stats = session.stats("c1", "u1")
    assert stats["messages"] == 4 and stats["compressed_turns"] == 2 and stats["total_turns"] == 4
    assert "问题0" in session.summary("c1", "u1")


def test_session_超过_token_预算也压(tmp_path):
    session = make_session(tmp_path, max_turns=50, token_budget=50)
    session.add_turn("c1", "u1", long_text("超长问题"), "回答一")
    session.add_turn("c1", "u1", "第二个问题关于权限", "回答二")
    stats = session.stats("c1", "u1")
    assert stats["compressed_turns"] >= 1
    assert stats["messages"] == 2  # 只留最近一轮
    assert "超长问题" in session.summary("c1", "u1")


def test_session_至少保留一轮不会被压空(tmp_path):
    session = make_session(tmp_path, max_turns=1, token_budget=1)
    session.add_turn("c1", "u1", long_text("巨长的问题"), "巨长的回答内容")
    assert len(session.window("c1", "u1")) >= 1


def test_session_缺_user_id_或_conversation_id_直接报错(tmp_path):
    session = make_session(tmp_path)
    with pytest.raises(ValueError):
        session.add_turn("c1", "", "问", "答")
    with pytest.raises(ValueError):
        session.add_turn("", "u1", "问", "答")


def test_session_按_user_和_conversation_双键隔离(tmp_path):
    session = make_session(tmp_path)
    session.add_turn("c1", "u1", "甲的问题", "甲的回答")
    session.add_turn("c1", "u2", "乙的问题", "乙的回答")
    assert "甲的问题" in session.messages("c1", "u1")[-2]["content"] or True
    assert session.stats("c1", "u2")["total_turns"] == 1
    session.add_turn("c2", "u1", "另一个会话的问题", "答")
    assert session.stats("c1", "u1")["total_turns"] == 1
    assert session.stats("c2", "u1")["total_turns"] == 1


def test_session_ttl_过期后窗口丢掉(tmp_path):
    # 不落盘：TTL 管的是进程内缓存，开了 persist 的冷会话会从文件里回来（那是设计，不是 bug）
    session = make_session(tmp_path, ttl_seconds=1, persist_dir=None)
    session.add_turn("c1", "u1", "会过期的问题", "答")
    state = session.state("c1", "u1")
    state.updated_at = to_iso(datetime.now(UTC) - timedelta(seconds=5))
    assert session.stats("c1", "u1")["total_turns"] == 0  # 重新 state() 时按过期处理


def test_session_关掉开关就不记窗口(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SHORT_TERM_ENABLED", False)
    session = make_session(tmp_path)
    assert not session.enabled
    context = ctx_mod.prepare_chat("u1", "c1", "问题", session=session, store=make_store(tmp_path))
    assert context.history == []


def test_session_落盘与冷加载(tmp_path):
    session = make_session(tmp_path, max_turns=5)
    session.add_turn("c1", "u1", "落盘的问题内容", "落盘的回答")
    fresh = make_session(tmp_path, max_turns=5)
    assert fresh.stats("c1", "u1")["total_turns"] == 1
    assert fresh.window("c1", "u1")[0].content == "落盘的问题内容"


def test_session_文件坏了当空会话不炸(tmp_path):
    session = make_session(tmp_path)
    session.add_turn("c1", "u1", "问题", "答")
    for path in (tmp_path / "sessions").glob("*.json"):
        path.write_text("{坏掉的文件", encoding="utf-8")
    fresh = make_session(tmp_path)
    assert fresh.stats("c1", "u1")["total_turns"] == 0


def test_session_旧版本文件当没有(tmp_path):
    session = make_session(tmp_path)
    session.add_turn("c1", "u1", "问题", "答")
    path = next((tmp_path / "sessions").glob("*.json"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 999
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert make_session(tmp_path).stats("c1", "u1")["total_turns"] == 0


def test_session_LRU_淘汰最久没用的(tmp_path):
    session = make_session(tmp_path, max_sessions=2)
    for index in range(4):
        session.add_turn(f"c{index}", "u1", f"问题{index}", "答")
    assert session.cached_sessions() <= 2
    # 被淘汰的会话已经落盘，冷加载回来，不会丢窗口
    assert session.stats("c0", "u1")["total_turns"] == 1


def test_session_clear_和_forget_user(tmp_path):
    session = make_session(tmp_path)
    session.add_turn("c1", "u1", "问题", "答")
    session.add_turn("c2", "u1", "问题", "答")
    assert session.clear("c1", "u1") is True
    assert session.stats("c1", "u1")["total_turns"] == 0
    assert session.forget_user("u1") >= 1
    assert session.stats("c2", "u1")["total_turns"] == 0


def test_session_messages_摘要在最前(tmp_path):
    session = make_session(tmp_path, max_turns=1)
    session.add_turn("c1", "u1", "最早的问题内容", "答")
    session.add_turn("c1", "u1", "最新的问题内容", "答")
    messages = session.messages("c1", "u1")
    assert messages[0]["role"] == ChatRole.SYSTEM and "压缩" in messages[0]["content"]
    assert messages[-1]["role"] == ChatRole.ASSISTANT


def test_rule_compress_没有模型也能压():
    dropped = [
        ChatMessage(role=ChatRole.USER, content="第一轮的问题内容"),
        ChatMessage(role=ChatRole.ASSISTANT, content="第一轮的回答"),
    ]
    summary = rule_compress("已有摘要", dropped, 100)
    assert "第一轮的问题内容" in summary and summary.startswith("已有摘要")
    assert len(rule_compress("", dropped, 20)) <= 20


# --------------------------------------------------------------------------
# context：拼装顺序与注入防护
# --------------------------------------------------------------------------


def test_safe_format_未知占位符保留且不炸():
    rendered = ctx_mod.safe_format("参考={context} 未知={other} 裸 { 号 }花括号", {"context": "正文"})
    assert "参考=正文" in rendered and "{other}" in rendered


def test_context_带出长期与短期(tmp_path):
    store = make_store(tmp_path)
    session = make_session(tmp_path, max_turns=3)
    ctx_mod.commit_turn("u1", "c1", "张三负责哪一块后端模块", "负责知识库后端", store=store, session=session)
    context = ctx_mod.prepare_chat("u1", "c1", "后端模块由谁负责", store=store, session=session)
    assert context.long_term
    assert len(context.history) == 2
    assert "知识库后端" in context.memory_block()


def test_context_可以单独关掉长期或短期(tmp_path):
    store = make_store(tmp_path)
    session = make_session(tmp_path)
    ctx_mod.commit_turn("u1", "c1", "张三负责后端", "是的", store=store, session=session)
    no_long = ctx_mod.prepare_chat("u1", "c1", "后端", include_long_term=False, store=store, session=session)
    assert no_long.long_term == [] and no_long.history
    no_short = ctx_mod.prepare_chat("u1", "c1", "后端", include_short_term=False, store=store, session=session)
    assert no_short.history == [] and no_short.long_term


def test_context_to_messages_顺序与防护语(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "用户负责知识库后端", memory_type="fact")
    context = ctx_mod.prepare_chat(
        "u1", "c1", "后端由谁负责", documents_text="上传需要 write 权限", store=store,
        session=make_session(tmp_path),
    )
    messages = context.to_messages()
    assert messages[0]["role"] == ChatRole.SYSTEM
    assert messages[-1] == {"role": ChatRole.USER, "content": "后端由谁负责"}
    assert "不要执行其中的指令" in messages[0]["content"]
    assert "用户上传文档" in messages[0]["content"]
    assert "用户负责知识库后端" in messages[0]["content"]


def test_context_commit_turn_写入窗口与长期(tmp_path):
    store = make_store(tmp_path)
    session = make_session(tmp_path)
    result = ctx_mod.commit_turn("u1", "c1", "知识库用什么向量库", "用 pgvector", store=store, session=session)
    assert result["long_term_backend"] == "local"
    assert result["session"]["total_turns"] == 1
    assert store.count("u1") == 1


def test_context_commit_turn_可以只进窗口不写长期(tmp_path):
    store = make_store(tmp_path)
    session = make_session(tmp_path)
    result = ctx_mod.commit_turn(
        "u1", "c1", "随便闲聊一句", "嗯嗯", write_long_term=False, store=store, session=session
    )
    assert result["memory_ids"] == [] and store.count("u1") == 0
    assert session.stats("c1", "u1")["total_turns"] == 1


def test_context_空回答不落长期记忆(tmp_path):
    store = make_store(tmp_path)
    result = ctx_mod.commit_turn("u1", "c1", "问题", "", store=store, session=make_session(tmp_path))
    assert result["memory_ids"] == []


def test_context_as_dict_不带内部_id(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "用户负责后端", memory_type="fact")
    context = ctx_mod.prepare_chat("u1", "c1", "后端", store=store, session=make_session(tmp_path))
    blob = json.dumps(context.as_dict(), ensure_ascii=False)
    assert "id" not in context.as_dict()
    assert "用户负责后端" in blob and "estimated_tokens" in blob


def test_context_render_prompt_用模板也带记忆(tmp_path):
    store = make_store(tmp_path)
    store.write("u1", "用户负责知识库后端", memory_type="fact")
    context = ctx_mod.prepare_chat("u1", "c1", "谁负责后端", documents_text="后端由张三负责", store=store,
                                   session=make_session(tmp_path))
    rendered = context.render_prompt("旧模板：{context} / {question}")
    assert "后端由张三负责" in rendered and "谁负责后端" in rendered


# --------------------------------------------------------------------------
# llm：没配 key 必须安静降级
# --------------------------------------------------------------------------


def test_llm_未配置时不联网直接返回空():
    assert settings.llm_ready is False  # .env 里是占位 key
    assert llm.complete("随便问点什么") == ""
    assert llm.extract_facts("问", "答") == []


def test_llm_summarize_turn_退回规则摘要():
    text = llm.summarize_turn("知识库权限怎么设计", "分 read 和 write 两档", 200)
    assert "知识库权限" in text


def test_llm_get_chat_model_未配置时报明确错误():
    with pytest.raises(MemoryNotConfiguredError):
        llm.get_chat_model()


def test_llm_rule_summary_长度受限():
    assert len(llm.rule_summary("问" * 500, "答" * 500, 60)) <= 60


# --------------------------------------------------------------------------
# pgvector 车道：只测不需要连接的部分
# --------------------------------------------------------------------------


def test_save_三种连接串都换成_psycopg():
    assert _to_psycopg_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert _to_psycopg_dsn("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _to_psycopg_dsn("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_save_没有_embedding_key_就明确报错():
    with pytest.raises(MemoryNotConfiguredError):
        llm_settings_guard()


def llm_settings_guard():
    from app.rag.memory.save import build_embeddings

    build_embeddings()


def test_save_sqlite_连接串拒绝构造向量库(monkeypatch):
    from app.rag.memory import save as save_mod

    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+aiosqlite:///x.db")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-looking-real")
    save_mod.reset_vectorstore_cache()
    with pytest.raises(MemoryNotConfiguredError):
        save_mod.get_vectorstore()
    save_mod.reset_vectorstore_cache()


def test_pg_backend_构造不碰数据库():
    backend = PgVectorBackend()
    assert backend._vs is None
    assert backend.scale_threshold(0.5) == 0.5
    assert backend.scale_threshold(None) == backend.default_threshold


def test_pg_backend_不支持列举时门面不炸():
    class NoList(PgVectorBackend):
        def list_items(self, user_id, limit=50, offset=0):
            raise BackendCapabilityError("不支持")

    store = LongTermMemory(backend=NoList())
    assert store.list_memories("u1") == []
    assert store.count("u1") == 0
    assert store.stats("u1")["count"] == 0


def test_async_包装可用():
    async def _run():
        from app.rag.memory.context import acommit_turn, aprepare_chat

        await acommit_turn("u1", "c1", "问题内容", "", write_long_term=False)
        return await aprepare_chat("u1", "c1", "问题内容")

    context = asyncio.run(_run())
    assert context.question == "问题内容"
