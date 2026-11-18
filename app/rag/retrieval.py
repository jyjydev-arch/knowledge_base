"""问答的检索层（M4 的第一版：**关键词车道**，形状与以后的向量版完全一致）

为什么先做关键词而不是直接上 pgvector：
  embedding 厂商/模型/维度还没定（维度一旦建列就不能改，见 config.EMBEDDING_DIMS 的注释），
  而前端联调现在就 Needs 参考资料。所以这一版只做「DB 里 LIKE + 词频排序」，
  但**返回形状按向量版定**（RetrievedContext: text + sources + mode），
  M4 换成向量/混合检索时只改本文件内部，路由和前端一行都不用动。

三条硬规矩：
  1. **必须有范围**。kb_ids 由调用方给出，且必须是「这个用户读得到的库」
     （app/services/kb_access.accessible_kb_ids 或会话自己限定的那个库）。
     这里不接受"kb_ids 为空=全库检索"这种默认：越权是这类系统最常见的洞，
     默认值必须是"查不到"而不是"全给你"。
  2. **进 prompt 的资料长度有上限**（CHAT_RETRIEVAL_MAX_CHARS）。一个 8k 字的块
     会把短期窗口整个吃掉，模型后面真正的问题反而看不见。
  3. **每块都带出处**（document_id / chunk_id / page / heading），
     前端的引用角标和"点回原文"全靠它；出处对不上的检索结果等于没有出处。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.crud import chunk_crud

# 英文/数字词：>=2 个字符才有意义（"a"、"I" 这种单字母命中率高得没边，纯噪声）
_ASCII_TERM = re.compile(r"[A-Za-z0-9_]{2,}")
# 中日韩连续段：没有分词器，所以按「整段 + 2-gram」造候选词
_CJK_RUN = re.compile(r"[一-鿿㐀-䶿]+")

MAX_QUERY_TERMS = 12          # 送给 SQL 的词数（与 chunk_crud.search_chunks 的上限一致）
HITS_PER_DOCUMENT = 2         # 同一篇文档最多贡献几块，避免一篇长文霸占整个参考资料
SCORE_REPEAT_CAP = 5          # 同一个词出现 5 次以上不再加分，防止长块靠体量刷分


@dataclass(slots=True)
class RetrievedContext:
    """一次检索的结果。text 进 prompt，sources 给前端"""

    text: str = ""
    sources: list[dict] = field(default_factory=list)
    mode: str = "none"          # keyword / disabled / no_scope / no_terms / none
    terms: list[str] = field(default_factory=list)
    candidate_total: int = 0    # DB 里命中的候选块数（截断前）

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "terms": list(self.terms),
            "candidates": self.candidate_total,
            "hits": len(self.sources),
            "chars": len(self.text),
        }


def extract_terms(question: str, *, max_terms: int = MAX_QUERY_TERMS) -> list[str]:
    """从问题里造检索词（纯函数，可单测）

    中文按 2-gram 展开是有意的：没有分词器时，"报销流程怎么走" 整串 ILIKE 几乎必然 0 命中，
    而 "报销"/"销流"/"流程" 能命中。噪声词（2-gram 里的"销流"）不致命——
    它只影响候选集大小，真正的取舍在 score 里按命中数与词长决定。
    """
    text = (question or "").strip()
    if not text:
        return []

    terms: list[str] = []
    seen: set[str] = set()

    def _push(word: str) -> None:
        token = word.strip().lower()
        if len(token) >= 2 and token not in seen and len(terms) < max_terms:
            seen.add(token)
            terms.append(token)

    for match in _ASCII_TERM.findall(text):
        _push(match)

    for run in _CJK_RUN.findall(text):
        if len(run) <= 4:
            _push(run)
            continue
        _push(run[:6])  # 长串的开头往往就是主题词
        for index in range(len(run) - 1):
            _push(run[index:index + 2])
            if len(terms) >= max_terms:
                break

    # 词长的排在前面：SQL 里只取前 MAX_QUERY_TERMS 个，长的更有信息量
    terms.sort(key=lambda item: (-len(item), item))
    return terms


def score_content(content: str, terms: list[str], *, heading: str = "", title: str = "") -> float:
    """词频算分：命中次数 × 词长权重，标题/小标题命中额外加权

    为什么不用真余弦：那是 embedding 的活。这里要的是"资料里有没有这几个字"，
    一个便宜、确定、能单测的启发式比半吊子的 TF-IDF 更好维护。
    """
    body = (content or "").lower()
    meta = f"{heading or ''} {title or ''}".lower()
    score = 0.0
    for term in terms:
        hits = body.count(term)
        if hits:
            score += (1.0 + len(term)) * min(hits, SCORE_REPEAT_CAP)
        if term in meta:
            score += 2.0 * (1.0 + len(term))
    return score


def rank_hits(hits: list[dict], terms: list[str], *, top_k: int) -> list[dict]:
    """排序 + 每篇文档限流 + 截断（纯函数）"""
    scored = []
    for index, hit in enumerate(hits):
        value = score_content(
            hit.get("content") or "",
            terms,
            heading=hit.get("heading") or "",
            title=hit.get("document_title") or hit.get("document_filename") or "",
        )
        scored.append((value, index, hit))  # index 兜底：同分时保持 DB 返回顺序，结果可复现
    scored.sort(key=lambda item: (-item[0], item[1]))

    picked: list[dict] = []
    per_document: dict[str, int] = {}
    for value, _index, hit in scored:
        if value <= 0:
            continue  # 一个词都没命中（只有标题命中过之类）就别进 prompt
        doc_key = str(hit.get("document_id"))
        if per_document.get(doc_key, 0) >= HITS_PER_DOCUMENT:
            continue
        per_document[doc_key] = per_document.get(doc_key, 0) + 1
        enriched = dict(hit)
        enriched["score"] = round(value, 3)
        picked.append(enriched)
        if len(picked) >= top_k:
            break
    return picked


def format_references(picked: list[dict], *, max_chars: int, snippet_chars: int) -> tuple[str, list[dict]]:
    """拼【参考资料】正文 + 引用列表。超预算从后面往前丢，但至少留一块

    至少要留一块是有意为之：全丢了 mode 还是 keyword 却什么资料都没有，
    模型会直接回答"资料里找不到"，用户以为检索坏了。
    """
    blocks: list[str] = []
    sources: list[dict] = []
    used = 0

    for index, hit in enumerate(picked, 1):
        content = str(hit.get("content") or "").strip()
        snippet = content[:snippet_chars]
        if len(content) > snippet_chars:
            snippet += "…"
        located = [f"[{index}]《{hit.get('document_title') or hit.get('document_filename') or '文档'}》"]
        if hit.get("page"):
            located.append(f"第{hit['page']}页")
        if hit.get("heading"):
            located.append(str(hit["heading"]))
        block = " · ".join(located) + "\n" + snippet

        if blocks and used + len(block) > max_chars:
            break  # 预算用完：后面更好的块没有了，但至少前面的还在
        blocks.append(block)
        used += len(block)
        sources.append(
            {
                "index": index,
                "document_id": str(hit.get("document_id")),
                "chunk_id": str(hit.get("chunk_id")),
                "kb_id": str(hit.get("kb_id")),
                "chunk_seq": int(hit.get("seq") or 0),
                "page": hit.get("page"),
                "heading": hit.get("heading"),
                "title": hit.get("document_title") or hit.get("document_filename") or "",
                "score": hit.get("score", 0),
                "snippet": snippet,
            }
        )
    return "\n\n".join(blocks), sources


async def retrieve(
    question: str,
    *,
    kb_ids: list[Any] | None,
    db: AsyncSession,
    top_k: int | None = None,
) -> RetrievedContext:
    """按可读库范围检索参考资料。

    返回永远是 RetrievedContext（可能是空的 text + mode 说明为什么空），**不抛异常**：
    检索没命中是常态，不该让一次问答失败；真出问题是 DB 挂了，那由上层的事务处理。
    """
    limit = settings.CHAT_RETRIEVAL_TOP_K if top_k is None else int(top_k)
    scope = [item for item in (kb_ids or []) if item is not None]

    if not settings.CHAT_RETRIEVAL_ENABLED:
        return RetrievedContext(mode="disabled")
    if not scope:
        # 没有范围就是没有资料。"全库检索"这个默认值永远不该存在
        return RetrievedContext(mode="no_scope")

    terms = extract_terms(question)
    if not terms:
        return RetrievedContext(mode="no_terms", terms=terms)

    hits = await chunk_crud.search_chunks(scope, terms, db, candidate_limit=max(limit * 10, 40))
    ranked = rank_hits(hits, terms, top_k=limit)
    text, sources = format_references(
        ranked,
        max_chars=settings.CHAT_RETRIEVAL_MAX_CHARS,
        snippet_chars=settings.CHAT_SNIPPET_CHARS,
    )
    return RetrievedContext(
        text=text,
        sources=sources,
        mode="keyword" if text else "none",
        terms=terms,
        candidate_total=len(hits),
    )


__all__ = ["RetrievedContext", "extract_terms", "format_references", "rank_hits", "retrieve", "score_content"]
