import uuid
from datetime import datetime
from sqlalchemy import String, ForeignKey, DateTime, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

class User(Base):
    __tablename__ = "users"

    # 对应: id UUID PRIMARY KEY DEFAULT gen_random_uuid()
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4,          # 插入时由 Python 生成，不依赖数据库方言
        server_default=text("gen_random_uuid()")
    )
    # 对应: username VARCHAR(50) UNIQUE NOT NULL
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    # 对应: email VARCHAR(255) UNIQUE NOT NULL
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # 对应: hashed_password VARCHAR(255) NOT NULL
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # 对应: created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=text("CURRENT_TIMESTAMP")
    )

    # 定义 ORM 关系（方便后续用 user.tokens 或 user.documents 直接查出关联数据）
    tokens: Mapped[list["Token"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Token(Base):
    __tablename__ = "token"

    # 对应: token VARCHAR(255) PRIMARY KEY
    token: Mapped[str] = mapped_column(String(255), primary_key=True)
    # 对应: expired_at TIMESTAMP WITH TIME ZONE NOT NULL
    expired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 对应: created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=text("CURRENT_TIMESTAMP")
    )
    # 对应: user_id UUID REFERENCES users(id) ON DELETE CASCADE
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="tokens")


class Document(Base):
    __tablename__ = "documents"

    # 对应: id UUID PRIMARY KEY DEFAULT gen_random_uuid()
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4,          # 插入时由 Python 生成，不依赖数据库方言
        server_default=text("gen_random_uuid()")
    )
    # 对应: user_id UUID REFERENCES users(id) ON DELETE CASCADE
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )
    # 对应: filename VARCHAR(255) NOT NULL
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 对应: status VARCHAR(20) DEFAULT 'pending'
    # 注意：这里用了 server_default，意味着通过 SQLAlchemy 插入时如果不传 status，数据库会自动填 'pending'
    status: Mapped[str] = mapped_column(String(20), server_default="pending", nullable=False)
    # 对应: created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=text("CURRENT_TIMESTAMP")
    )

    user: Mapped["User"] = relationship(back_populates="documents")