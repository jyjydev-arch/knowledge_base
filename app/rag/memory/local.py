"""长期记忆的本地后端：JSONL 文件 + 词面 hash 向量

为什么要有它：pgvector 车道得先有 PG 库 + pgvector 扩展 + 能联网的 embedding 服务，
这三样现在一台都没有。没有这层，记忆逻辑就只能「写完等建库那天再第一次跑」，
那是最容易翻车的节奏。有了它，agent / 前端联调、单元测试都能今天就跑通。

诚实说明它的局限（所以生产必须切 pgvector）：
  - 相似度是**词面重叠**（n-gram hash），不是语义。"如何重置密码" 检不到 "忘记口令怎么办"。
  - 全量线性扫描，几万条会慢；PG 那边有 HNSW 索引。
  - 阈值口径和真向量不一样，MEMORY_SCORE_THRESHOLD 在这里要调低些（默认给 0.35）。
接口和 pgvector 后端完全一致，上层代码一行都不用改。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from pathlib import Path

from app.rag.memory.base import MemoryBackend, MemoryItem, safe_segment

LOCAL_EMBEDDING_DIMS = 512  # 词面向量的维度，和真 embedding 维度无关，别和 EMBEDDING_DIMS 混
# 桶太少会撞车：256 维时「我住在北京」和「Markdown 会议」能算出 0.15 的假相似，512 维降到 ~0.1


def _tokens(text: str) -> list[tuple[str, float]]:
    """切成带权重的检索单元

    中文：2-gram 权重 1.0、3-gram 权重 0.5。**不要**把整段中文当一个词 ——
    "后端谁负责" 永远匹配不上 "用户负责知识库后端"，却会把权重摊薄，让真命中被淹没。
    英文/数字：按词，权重 1.0（大小写已归一）。
    """
    lowered = text.lower()
    units: list[tuple[str, float]] = []
    for word in "".join(c if c.isalnum() and not _is_cjk(c) else " " for c in lowered).split():
        units.append((word, 1.0))
    chars = [c for c in lowered if _is_cjk(c)]
    units.extend(("".join(chars[i : i + 2]), 1.0) for i in range(len(chars) - 1))
    units.extend(("".join(chars[i : i + 3]), 0.5) for i in range(len(chars) - 2))
    return units


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff" or "\u3400" <= char <= "\u4dbf"


def vectorize(text: str, dims: int = LOCAL_EMBEDDING_DIMS) -> dict[int, float]:
    """词面向量：检索单元 hash 进 dims 个桶，桶里存的是**权重上限 1.0**（出现即算，不按次数堆）

    不做 L2 归一化也不按词频堆：短问句对长条目时，这两种都会把分数稀释到没法用。
    """
    buckets: dict[int, float] = {}
    for token, weight in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        dim = int.from_bytes(digest, "big") % dims
        buckets[dim] = max(buckets.get(dim, 0.0), weight)
    return buckets


def lexical_similarity(query_vector: dict[int, float], doc_vector: dict[int, float]) -> float:
    """词面相似度 = 查询覆盖率 × 长度阻尼

    为什么不用余弦：query 往往就一句话（几个 n-gram），记忆条目是一大段，
    余弦会被长度差稀释到 0.0x，强相关也过不了阈值（第一版检索全空就是这么废掉的）。
    覆盖率直接问「这句话里的词，条目里出现了多少」，短问句对长条目也能给出正常分数；
    阻尼项负责把"条目巨长、什么都含一点"的假高分压下去。
    """
    if not query_vector or not doc_vector:
        return 0.0
    query_weight = sum(query_vector.values())
    if not query_weight:
        return 0.0
    overlap = sum(min(weight, doc_vector.get(dim, 0.0)) for dim, weight in query_vector.items())
    coverage = overlap / query_weight
    dampening = 1.0 / (1.0 + 0.015 * max(0.0, sum(doc_vector.values()) - 15.0))
    return round(coverage * dampening, 6)


class LocalJsonBackend(MemoryBackend):
    """每个用户一个 JSONL 文件：data/memory/local/{user_id}.jsonl

    - 读：首次访问某用户时整份载入内存，之后命中缓存（单机 dev 规模无所谓）
    - 写：追加语义但整份重写，保证 prune/delete 之后的文件是干净的
    - 并发：进程内用锁串行；真要多人同时写，请用 pgvector 后端（有事务和索引）
    """

    name = "local"
    default_threshold = 0.25   # 覆盖率口径：强相关 0.6+， topical 重叠 0.25~0.5
    threshold_scale = 0.50

    def __init__(
        self,
        root: str | Path = "data/memory/local",
        dedupe_threshold: float = 0.95,
        dims: int = LOCAL_EMBEDDING_DIMS,
    ) -> None:
        self.root = Path(root)
        self.dedupe_threshold = dedupe_threshold
        self.dims = dims
        self._cache: dict[str, tuple[list[MemoryItem], list[dict[int, float]]]] = {}
        self._lock = threading.RLock()

    # ---------- 文件读写 ----------
    def path_for(self, user_id: str) -> Path:
        return self.root / f"{safe_segment(user_id)}.jsonl"

    def _load(self, user_id: str) -> tuple[list[MemoryItem], list[dict[int, float]]]:
        key = str(user_id)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            items: list[MemoryItem] = []
            path = self.path_for(key)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 半行/手工改坏的文件，跳过而不是整个后端起不来
                    text = str(raw.get("text") or "")
                    if not text:
                        continue
                    item = MemoryItem.from_dict(raw)
                    items.append(item)
            vectors = [vectorize(item.text, self.dims) for item in items]
            self._cache[key] = (items, vectors)
            return items, vectors

    def _dump(self, user_id: str, items: Sequence[MemoryItem]) -> None:
        path = self.path_for(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in items),
            encoding="utf-8",
        )
        tmp.replace(path)  # 原子替换，写一半崩了也不会留下坏文件

    # ---------- 接口实现 ----------
    def add(self, items: Sequence[MemoryItem], dedupe_threshold: float | None = None) -> list[str]:
        dedupe = self.dedupe_threshold if dedupe_threshold is None else dedupe_threshold
        stored: list[str] = []
        for item in items:
            if not item.text.strip():
                continue
            with self._lock:
                bucket, vectors = self._load(item.user_id)
                # 完全同文直接跳过；再和已有记忆比相似度，太像的也跳过（防"每轮都存一遍"灌水）
                if any(existing.text.strip() == item.text.strip() for existing in bucket):
                    continue
                vector = vectorize(item.text, self.dims)
                scores = [lexical_similarity(vector, old) for old in vectors]
                if scores and max(scores) >= dedupe:
                    continue
                bucket.append(item)
                vectors.append(vector)
                self._dump(item.user_id, bucket)
            stored.append(item.id)
        return stored

    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
        threshold: float | None = None,
        memory_type: str | None = None,
    ) -> list[MemoryItem]:
        if threshold is None:
            threshold = self.default_threshold
        qvec = vectorize(query, self.dims)
        bucket, vectors = self._load(str(user_id))
        scored: list[MemoryItem] = []
        for item, vector in zip(bucket, vectors, strict=True):
            if memory_type and item.memory_type != memory_type:
                continue
            score = lexical_similarity(qvec, vector)
            if score < threshold:
                continue
            scored.append(MemoryItem.from_dict({**item.to_dict(), "score": score}))
        # 相似度一样的情况下，新的那条更可能还有用，所以次级排序按 created_at 倒序
        scored.sort(key=lambda item: (item.score, item.created_at), reverse=True)
        return scored[: max(0, top_k)]

    def list_items(self, user_id: str, limit: int = 50, offset: int = 0) -> list[MemoryItem]:
        bucket, _ = self._load(str(user_id))
        # reversed 先翻转再稳定排序：时间戳打平时，后写入的那条排前面（"新"的正确定义）
        newest_first = sorted(reversed(bucket), key=lambda item: item.created_at, reverse=True)
        return [MemoryItem.from_dict(item.to_dict()) for item in newest_first[offset : offset + limit]]

    def count(self, user_id: str) -> int:
        bucket, _ = self._load(str(user_id))
        return len(bucket)

    def delete(self, user_id: str, memory_ids: Sequence[str]) -> int:
        ids = {str(memory_id) for memory_id in memory_ids}
        if not ids:
            return 0
        with self._lock:
            bucket, vectors = self._load(str(user_id))
            keep = [(item, vector) for item, vector in zip(bucket, vectors, strict=True) if item.id not in ids]
            removed = len(bucket) - len(keep)
            if removed:
                self._cache[str(user_id)] = ([item for item, _ in keep], [vector for _, vector in keep])
                self._dump(str(user_id), [item for item, _ in keep])
        return removed

    def clear(self) -> None:
        """清掉进程内缓存（测试之间互不串味用；不动磁盘文件）"""
        with self._lock:
            self._cache.clear()
