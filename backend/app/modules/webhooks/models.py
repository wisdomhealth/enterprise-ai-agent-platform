from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WebhookSubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class WebhookDeliveryState(StrEnum):
    PENDING = "PENDING"
    DELIVERING = "DELIVERING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "cardinality(event_types) > 0",
            name="ck_webhook_subscriptions_event_types_nonempty",
        ),
        CheckConstraint("version > 0", name="ck_webhook_subscriptions_version_positive"),
        ForeignKeyConstraint(
            ["organization_id", "created_by_id"],
            ["staff_users.organization_id", "staff_users.id"],
            name="fk_webhook_subscriptions_organization_creator",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_webhook_subscriptions_dispatch",
            "organization_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(150)), nullable=False)
    status: Mapped[WebhookSubscriptionStatus] = mapped_column(
        Enum(WebhookSubscriptionStatus, name="webhook_subscription_status"),
        nullable=False,
        default=WebhookSubscriptionStatus.ACTIVE,
        server_default=text("'ACTIVE'::webhook_subscription_status"),
    )
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_encrypted_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_key_version: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("delivery_attempt >= 0", name="ck_webhook_deliveries_attempt_nonnegative"),
        UniqueConstraint(
            "subscription_id", "event_id", name="uq_webhook_deliveries_subscription_event"
        ),
        UniqueConstraint("job_id", name="uq_webhook_deliveries_job"),
        Index(
            "ix_webhook_deliveries_recovery",
            "state",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("outbox_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("job_intents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[WebhookDeliveryState] = mapped_column(
        Enum(WebhookDeliveryState, name="webhook_delivery_state"),
        nullable=False,
        default=WebhookDeliveryState.PENDING,
        server_default=text("'PENDING'::webhook_delivery_state"),
    )
    delivery_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_summary: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
