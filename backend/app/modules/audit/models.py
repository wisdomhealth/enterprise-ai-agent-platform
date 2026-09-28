from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_scope_occurred_at", "organization_id", "occurred_at"),
    )

    # 审计事件的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    # 执行当前操作的主体唯一标识。
    actor_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    # 触发当前记录的业务操作名称。
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    # 被操作业务对象的类型。
    object_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # 被操作业务对象的唯一标识。
    object_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    # 操作执行结果。
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    # 操作相关的结构化详情。
    details: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    # 事件实际发生时间。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
