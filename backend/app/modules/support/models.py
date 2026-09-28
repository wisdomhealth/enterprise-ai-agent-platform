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

    # 人工接管记录的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 关联会话的唯一标识。
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 记录当前状态机状态。
    state: Mapped[ConversationState] = mapped_column(
        Enum(ConversationState, name="conversation_state", create_type=False), nullable=False
    )
    # 触发人工接管的原因类型。
    trigger: Mapped[HandoffTrigger] = mapped_column(
        Enum(HandoffTrigger, name="handoff_trigger"), nullable=False
    )
    # 命中的敏感主题；未命中时为空。
    sensitive_topic: Mapped[SensitiveTopic | None] = mapped_column(
        Enum(SensitiveTopic, name="sensitive_topic"), nullable=True
    )
    # 转交人工时保存的会话上下文快照。
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # 触发接管时最后一条客户消息序号。
    last_customer_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 当前负责处理接管事项的员工唯一标识。
    assigned_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 并发控制版本号，用于检测和阻止并发覆盖。
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # 人工接管事项解决时间；为空表示尚未解决。
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
