"""记忆层自检 CLI：不建库、不联网，先看两层记忆到底在干什么

用法：
    .venv/bin/python scripts/try_memory.py                     # 跑内置的三轮演示
    .venv/bin/python scripts/try_memory.py --turns "先问一句" "再问一句"
    .venv/bin/python scripts/try_memory.py --user u1 --dir /tmp/memdemo --max-turns 1

每轮打印三件最关键的事，方便判断配置是否合理：
  1. 这轮模型实际看到什么（长期记忆命中几条、摘要多长、窗口几轮）
  2. 短期窗口有没有被压缩、压掉的内容去了哪
  3. 长期记忆写了什么、下一轮能不能被检回来
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.rag.memory.context import commit_turn, prepare_chat  # noqa: E402
from app.rag.memory.local import LocalJsonBackend  # noqa: E402
from app.rag.memory.session import ShortTermMemory  # noqa: E402
from app.rag.memory.store import LongTermMemory  # noqa: E402

DEMO_TURNS = [
    ("我叫张三，负责知识库的后端模块", "好的，已记下你负责后端"),
    ("我们数据库用的是 PostgreSQL 加 pgvector", "明白，向量检索走 pgvector"),
    ("后端模块是谁在负责？", "根据长期记忆：张三负责后端模块"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆层自检")
    parser.add_argument("--user", default="demo_user")
    parser.add_argument("--conversation", default="demo_conv")
    parser.add_argument("--dir", default="data/memory/try")
    parser.add_argument("--max-turns", type=int, default=1, help="窗口保留几轮（调小更容易看到压缩）")
    parser.add_argument("--token-budget", type=int, default=200)
    parser.add_argument("--documents", default="", help="模拟检索到的参考资料")
    parser.add_argument("--turns", nargs="*", default=[], help="成对传入：问1 答1 问2 答2")
    args = parser.parse_args()

    if len(args.turns) % 2:
        print("--turns 必须成对（一问一答）", file=sys.stderr)
        return 2
    turns = [(args.turns[i], args.turns[i + 1]) for i in range(0, len(args.turns), 2)] or DEMO_TURNS

    store = LongTermMemory(backend=LocalJsonBackend(root=args.dir))
    session = ShortTermMemory(max_turns=args.max_turns, token_budget=args.token_budget,
                              persist_dir=None, ttl_seconds=3600)

    print(f"后端：长期={store.backend_name} 短期=内存窗口 | LLM 已配置={settings.llm_ready}")
    print(f"目录：{Path(args.dir).resolve()}")
    print()

    for index, (question, answer) in enumerate(turns, 1):
        context = prepare_chat(args.user, args.conversation, question,
                               documents_text=args.documents, store=store, session=session)
        result = commit_turn(args.user, args.conversation, question, answer, store=store, session=session)
        stats = result["session"]
        print(f"===== 第 {index} 轮 =====")
        print(f"问：{question}")
        print(f"长期记忆命中：{len(context.long_term)} 条"
              + (f" -> {[item.text[:24] for item in context.long_term]}" if context.long_term else ""))
        print(f"窗口：{stats.get('messages', 0)} 条消息 / 累计 {stats.get('total_turns', 0)} 轮 / "
              f"已压缩 {stats.get('compressed_turns', 0)} 轮 / 预算内 {stats.get('window_tokens', 0)} tokens")
        if context.summary:
            print(f"早期摘要：{context.summary[:80]}")
        print(f"写入长期记忆：{len(result['memory_ids'])} 条")
        print(f"发给模型的消息数：{len(context.to_messages())} (估算 {context.token_estimate()} tokens)")
        print()

    print("===== 收尾状态 =====")
    print("该用户长期记忆条数：", store.count(args.user))
    print(json.dumps(store.stats(args.user), ensure_ascii=False, indent=2))
    print("\n下一轮问答会带上上面这些长期记忆（相关才带，不相关不塞）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
