"""数据库表定义 —— 表结构的唯一真源

约定（详见 DEVELOPMENT.md 第 5 节）:
    1. 主键统一 uuid，并且在 Python 端就生成（default=uuid.uuid4），
       不依赖数据库方言函数，换库/跑 SQLite 测试都能用；
    2. 时间统一 DateTime(timezone=True) + server_default CURRENT_TIMESTAMP；
    3. 属于用户的数据一律冗余一个 owner_id，列表/检索只加一个 WHERE 就能隔离，
       不用为了鉴权去 join 父表（越权是这类系统最容易出的洞，宁可冗余）；
    4. 建表用 scripts/init_db.py（由本文件生成），init_db.sql 只是它的一份可读快照；
    5. 状态字段用 varchar + 本文件里的 StrEnum 常量，不建数据库枚举类型
       （PG 的 CREATE TYPE 改值很痛，varchar + CHECK/常量更灵活）；
    6. 还没做的事：
       - chat_history（会话/消息表）→ TODO.md P0-M5 再建
       - chunks.embedding 向量列 + pgvector → TODO.md P0-M3，方案定了再加
"""

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# 主键类型集中定义，换库时只改这一处。
# 用 SQLAlchemy 2.0 的通用 Uuid（不是 PG 专有的 postgresql.UUID）：
#   PG 上渲染成原生 uuid，SQLite 上自动退化成 CHAR(32) 并负责 str<->uuid 转换，
#   这样测试库和线上库都能用同一份模型。
UUID_TYPE = sa.Uuid(as_uuid=True)
# json 列：PG 用 jsonb，其它库用 json
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class UserRole(StrEnum):
    """users.role；注册时命中 settings.ADMIN_USERNAMES 才会是 admin"""

    USER = "user"
    ADMIN = "admin"


class KbVisibility(StrEnum):
    """knowledge_bases.visibility"""

    PRIVATE = "private"  # 仅owner + 被共享的用户可见
    PUBLIC = "public"    # 全站可见（只读）


class DocStatus(StrEnum):
    """documents.status 流转: pending -> parsing -> chunking -> indexed / failed"""

    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    INDEXED = "indexed"
    FAILED = "failed"


class TimestampMixin:
    """created_at / updated_at；updated_at 由 SQLAlchemy 在 UPDATE 时刷新"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class SoftDeleteMixin:
    """软删除：NULL 表示未删除，查询一律 WHERE deleted_at IS NULL"""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base, TimestampMixin, SoftDeleteMixin):
    """账号表。注意：注销目前走硬删除（级联清掉库/文档/块），软删列先留着"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # 权限：先靠 role 区分，取代之前 .env 里 ADMIN_USERNAMES 的判断
    role: Mapped[str] = mapped_column(
        String(20), default=UserRole.USER, server_default=text("'user'"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tokens: Mapped[list["Token"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    knowledge_bases: Mapped[list["KnowledgeBase"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


class Token(Base):
    """已签发的登录凭证

    这里存的不是 JWT 原文而是它的 sha256（token_hash）：
    数据库被拖走也拿不去直接可用的凭证；校验时把请求带上来的 token 哈希后查。
    """

    __tablename__ = "tokens"
    __table_args__ = (
        Index("ix_tokens_user_expires", "user_id", "expires_at"),
        Index("ix_tokens_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 最长 45
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="tokens")


class KnowledgeBase(Base, TimestampMixin, SoftDeleteMixin):
    """知识库（总表）：一个用户可建多个库，文档挂在库下面

    description 就是这个库的「具体说明」：装什么内容、给谁用、口径怎么定，
    后面做问答时可以直接把它拼进 prompt，让模型知道资料范围。
    """

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_kb_owner_name"),
        Index("ix_kb_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, default="", server_default=text("''"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        String(16), default=KbVisibility.PRIVATE, server_default=text("'private'"), nullable=False
    )

    # ---- 入库配置（解析/切块参数，M1-M2 用；向量方案定了再填下面两项）----
    chunk_size: Mapped[int] = mapped_column(Integer, default=800, server_default=text("800"), nullable=False)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=120, server_default=text("120"), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 例: bge-m3
    embedding_dim: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)   # 必须和向量列维度一致

    # ---- 统计与版本：文档增删/重索引时由 document_crud.bump_kb_version() 一起更新 ----
    #      version 每次内容变化 +1，前端可据此判断要不要刷新缓存；检索结果也带上它做审计
    document_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["User"] = relationship(back_populates="knowledge_bases")
    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Document(Base, TimestampMixin, SoftDeleteMixin):
    """上传的原始文件（一行 = 一个文件）"""

    __tablename__ = "documents"
    __table_args__ = (
        # 同一个库里同一个文件不重复入库（sha256 判重）
        UniqueConstraint("kb_id", "file_hash", name="uq_document_kb_file_hash"),
        Index("ix_document_owner_status", "owner_id", "status"),
        Index("ix_document_kb_created", "kb_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余：鉴权过滤只认它
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)     # 展示用
    filename: Mapped[str] = mapped_column(String(255), nullable=False)  # 用户上传时的原始名
    # 相对路径，约定 data/{user_id}/documents/{document_id}.{ext}（沿用 documentrouter 里的说明）
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex

    status: Mapped[str] = mapped_column(
        String(16), default=DocStatus.PENDING, server_default=text("'pending'"), nullable=False
    )
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    """切块后的正文，检索的基本单位

    还没有 embedding 列（用户已确认 pgvector 先不处理）：等向量化方案定了再加
    vector(N) 列 + HNSW 索引，并同步 knowledge_bases.embedding_model/embedding_dim。
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "seq", name="uq_chunk_document_seq"),
        Index("ix_chunk_owner_kb", "owner_id", "kb_id"),
        Index("ix_chunk_kb", "kb_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 块在文档内的序号，从 0 开始
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)

    # 定位信息，回答时好标出处
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 不叫 metadata 是为了避开 SQLAlchemy 的 Base.metadata；库里列名同样用 extra
    extra: Mapped[dict] = mapped_column(
        JSON_TYPE, default=dict, server_default=text("'{}'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")
