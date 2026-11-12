"""长期记忆的 PGVector 车道：向量库与 embedding 客户端的初始化

和 local 后端是同一个接口，只是把「词面 hash」换成「真语义向量」。
用 lru_cache 保成单例：PGVector 在构造时就会连库、建扩展、建表、建 collection，
每轮对话 new 一个等于每轮建一次表，绝对不能那样用。

三个容易踩的点，写死在这里：
  1. langchain_postgres.PGVector 是**同步**实现，驱动必须是 psycopg v3，
     所以要把 settings.DATABASE_URL 里的 asyncpg 前缀换掉（见 _to_psycopg_dsn）。
  2. pgvector 扩展和 embedding 维度是**绑死**的：`dimensions` 一旦建过 collection 就不能改，
     换模型必须连 EMBEDDING_DIMS 和 collection 一起换，否则插入时报维度不一致。
  3. DeepSeek 没有 embedding 接口。对话模型和向量模型是两件事，各自配各家：
     LLM_API_KEY / LLM_BASE_URL 走对话，EMBEDDING_API_KEY / EMBEDDING_BASE_URL 走向量。
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.rag.memory.base import MemoryNotConfiguredError


def _to_psycopg_dsn(url: str) -> str:
    """把各种写法统一成 SQLAlchemy + psycopg v3 的同步连接串

    postgresql+asyncpg://... → postgresql+psycopg://...
    postgres://...           → postgresql+psycopg://...
    postgresql://...         → postgresql+psycopg://...
    其余部分（主机、端口、库名、参数）原样保留。
    """
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def build_embeddings():
    """按配置造 embedding 客户端（没有 key 就明确报错，不要静默走 OpenAI 默认值）"""
    from langchain_openai import OpenAIEmbeddings

    if not settings.embedding_ready:
        raise MemoryNotConfiguredError(
            "长期记忆（pgvector 车道）需要 embedding 服务：请在 .env 里配 EMBEDDING_API_KEY"
            "（DeepSeek 没有 embedding 接口，EMBEDDING_BASE_URL 要指向提供 embedding 的那一家）"
        )

    kwargs = {
        "model": settings.EMBEDDING_MODEL,
        "api_key": settings.EMBEDDING_API_KEY,
        "timeout": settings.LLM_TIMEOUT_SECONDS,
    }
    if settings.EMBEDDING_BASE_URL:
        kwargs["openai_api_base"] = settings.EMBEDDING_BASE_URL
    # dimensions 只有 v3 系列支持；给老模型传这个参数会 400
    if settings.EMBEDDING_MODEL.startswith("text-embedding-3") and settings.EMBEDDING_DIMS:
        kwargs["dimensions"] = settings.EMBEDDING_DIMS
    return OpenAIEmbeddings(**kwargs)


@lru_cache(maxsize=1)
def get_vectorstore():
    """全局单例。首次调用才会真的连库建表，所以不要在 import 阶段碰它"""
    from langchain_postgres import PGVector
    from langchain_postgres.vectorstores import DistanceStrategy

    if settings.DATABASE_URL.startswith("sqlite"):
        raise MemoryNotConfiguredError(
            "pgvector 后端连不了 SQLite：请把 DATABASE_URL 换成 PostgreSQL（开发期可先用 MEMORY_BACKEND=local）"
        )

    return PGVector(
        embeddings=build_embeddings(),
        connection=_to_psycopg_dsn(settings.DATABASE_URL),
        collection_name=settings.MEMORY_PG_COLLECTION,
        distance_strategy=DistanceStrategy.COSINE,
        embedding_length=settings.EMBEDDING_DIMS,
        use_jsonb=True,        # metadata 存 JSONB，user_id 过滤才走得动
        create_extension=True,  # 首次用时顺手 CREATE EXTENSION vector（需要账号有权限）
        echo=False,
    )


def reset_vectorstore_cache() -> None:
    """换配置 / 跑测试后用一次，否则旧的单例会带着上一条连接的池子继续用"""
    get_vectorstore.cache_clear()
