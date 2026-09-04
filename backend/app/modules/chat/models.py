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

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[ConversationState] = mapped_column(
        Enum(ConversationState, name="conversation_state"),
        nullable=False,
        default=ConversationState.AI_ACTIVE,
        server_default=sql_text("'AI_ACTIVE'::conversation_state"),
    )
    customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sql_text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ChatSessionCredential(Base):
    __tablename__ = "chat_session_credentials"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_chat_session_credentials_token_hash"),
        Index("ix_chat_session_credentials_session", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_chat_messages_session_sequence"),
        Index("ix_chat_messages_session_sequence", "session_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[ChatActor] = mapped_column(
        Enum(ChatActor, name="chat_actor"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ChatMessageStatus] = mapped_column(
        Enum(ChatMessageStatus, name="chat_message_status"),
        nullable=False,
        default=ChatMessageStatus.PERSISTED,
        server_default=sql_text("'PERSISTED'::chat_message_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
