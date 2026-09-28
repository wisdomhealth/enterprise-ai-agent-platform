from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "publish_attempts >= 0", name="ck_outbox_events_publish_attempts_nonnegative"
        ),
        Index(
            "ix_outbox_events_pending",
            "occurred_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    # 事件的全局唯一标识。
    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 事件类型，用于选择下游处理逻辑。
    event_type: Mapped[str] = mapped_column(String(150), nullable=False)
    # 事件结构版本，用于兼容后续演进。
    event_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    # 事件所属聚合根的类型。
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # 事件所属聚合根的唯一标识。
    aggregate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    # 事件携带的结构化业务载荷。
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # 事件实际发生时间。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 事件成功投递到消息代理的时间。
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 事件向消息代理投递的累计尝试次数。
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    # 处理该事件的消费者名称。
    consumer_name: Mapped[str] = mapped_column(String(150), primary_key=True)
    # 事件的全局唯一标识。
    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    # 消费者确认完成事件处理的时间。
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
