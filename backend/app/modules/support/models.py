from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.chat.models import ConversationState


class SupportAction(StrEnum):
    REQUEST_HANDOFF = "REQUEST_HANDOFF"
    QUEUE = "QUEUE"
    CLAIM = "CLAIM"
    REPLY = "REPLY"
    RESOLVE = "RESOLVE"
    RESUME_AI = "RESUME_AI"
    TIMEOUT = "TIMEOUT"


class HandoffTrigger(StrEnum):
    CUSTOMER_REQUEST = "CUSTOMER_REQUEST"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    SENSITIVE_TOPIC = "SENSITIVE_TOPIC"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class SensitiveTopic(StrEnum):
    ACCOUNT_SECURITY = "ACCOUNT_SECURITY"
    PAYMENT_DATA = "PAYMENT_DATA"
    LEGAL_THREAT = "LEGAL_THREAT"
    SAFETY = "SAFETY"
    PRIVACY_REQUEST = "PRIVACY_REQUEST"


class Handoff(Base):
    __tablename__ = "support_handoffs"
    __table_args__ = (
        Index("ix_support_handoffs_session", "session_id", "created_at"),
        Index("ix_support_handoffs_queue", "organization_id", "state", "created_at"),
        Index("ix_support_handoffs_assignee", "assigned_user_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[ConversationState] = mapped_column(
        Enum(ConversationState, name="conversation_state", create_type=False), nullable=False
    )
    trigger: Mapped[HandoffTrigger] = mapped_column(
        Enum(HandoffTrigger, name="handoff_trigger"), nullable=False
    )
    sensitive_topic: Mapped[SensitiveTopic | None] = mapped_column(
        Enum(SensitiveTopic, name="sensitive_topic"), nullable=True
    )
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    last_customer_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
