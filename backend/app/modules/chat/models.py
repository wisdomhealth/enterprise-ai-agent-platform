from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ConversationState(StrEnum):
    AI_ACTIVE = "AI_ACTIVE"
    HANDOFF_REQUESTED = "HANDOFF_REQUESTED"
    QUEUED = "QUEUED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    RESOLVED = "RESOLVED"


class ChatActor(StrEnum):
    CUSTOMER = "CUSTOMER"
    AI = "AI"
    STAFF = "STAFF"
    SYSTEM = "SYSTEM"


class ChatMessageStatus(StrEnum):
    PERSISTED = "PERSISTED"


class ChatSSEEventType(StrEnum):
    """Customer-safe event names derived from durable chat message state."""

    MESSAGE_VALIDATED = "message.validated"
    MESSAGE_SEGMENT = "message.segment"
    SESSION_STATE = "session.state"
    ERROR_SAFE = "error.safe"


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_organization", "organization_id"),
        Index("ix_chat_sessions_knowledge_base", "knowledge_base_id"),
    )

    # 聊天会话的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所属知识库的唯一标识。
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 记录当前状态机状态。
    state: Mapped[ConversationState] = mapped_column(
        Enum(ConversationState, name="conversation_state"),
        nullable=False,
        default=ConversationState.AI_ACTIVE,
        server_default=sql_text("'AI_ACTIVE'::conversation_state"),
    )
    # 客户姓名；数据保留任务到期后会进行脱敏。
    customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 客户邮箱；数据保留任务到期后会进行脱敏。
    customer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # 并发控制版本号，用于检测和阻止并发覆盖。
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sql_text("1")
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ChatSessionCredential(Base):
    __tablename__ = "chat_session_credentials"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_chat_session_credentials_token_hash"),
        Index("ix_chat_session_credentials_session", "session_id"),
    )

    # 聊天会话凭证的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    # 关联会话的唯一标识。
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 会话令牌的安全哈希值，不保存令牌明文。
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 凭证或租约的过期时间。
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 凭证被撤销的时间；为空表示尚未撤销。
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_chat_messages_session_sequence"),
        Index("ix_chat_messages_session_sequence", "session_id", "sequence"),
    )

    # 聊天消息的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    # 关联会话的唯一标识。
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 消息在所属会话中的单调递增序号。
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 消息发送方类型。
    actor: Mapped[ChatActor] = mapped_column(
        Enum(ChatActor, name="chat_actor"), nullable=False
    )
    # 正文内容。
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 记录当前业务状态。
    status: Mapped[ChatMessageStatus] = mapped_column(
        Enum(ChatMessageStatus, name="chat_message_status"),
        nullable=False,
        default=ChatMessageStatus.PERSISTED,
        server_default=sql_text("'PERSISTED'::chat_message_status"),
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
