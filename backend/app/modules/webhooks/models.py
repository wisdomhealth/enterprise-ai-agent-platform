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

    # Webhook 订阅的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 创建该资源的员工唯一标识。
    created_by_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    # 接收事件通知的 Webhook HTTPS 地址。
    endpoint_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # 订阅方希望接收的事件类型集合。
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(150)), nullable=False)
    # 记录当前业务状态。
    status: Mapped[WebhookSubscriptionStatus] = mapped_column(
        Enum(WebhookSubscriptionStatus, name="webhook_subscription_status"),
        nullable=False,
        default=WebhookSubscriptionStatus.ACTIVE,
        server_default=text("'ACTIVE'::webhook_subscription_status"),
    )
    # Webhook 签名密钥的加密密文。
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 用于保护 Webhook 签名密钥的数据密钥密文。
    secret_encrypted_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 加密 Webhook 签名密钥时使用的随机数。
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Webhook 签名密钥使用的加密算法。
    secret_algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    # Webhook 签名主密钥版本。
    secret_key_version: Mapped[str] = mapped_column(String(512), nullable=False)
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

    # Webhook 投递记录的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 关联 Webhook 订阅的唯一标识。
    subscription_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 事件的全局唯一标识。
    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("outbox_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 关联持久化任务的唯一标识。
    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("job_intents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 记录当前状态机状态。
    state: Mapped[WebhookDeliveryState] = mapped_column(
        Enum(WebhookDeliveryState, name="webhook_delivery_state"),
        nullable=False,
        default=WebhookDeliveryState.PENDING,
        server_default=text("'PENDING'::webhook_delivery_state"),
    )
    # Webhook 当前投递尝试次数。
    delivery_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Webhook 最近一次响应的 HTTP 状态码。
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Webhook 最近一次响应的截断摘要。
    response_summary: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # 最近一次处理失败的标准错误码。
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Webhook 成功投递时间；为空表示尚未成功。
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
