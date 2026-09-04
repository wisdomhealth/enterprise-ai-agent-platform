from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ErasureScope(StrEnum):
    CUSTOMER = "CUSTOMER"
    KNOWLEDGE_DOCUMENT = "KNOWLEDGE_DOCUMENT"


class ErasureStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class ErasureTargetType(StrEnum):
    CHAT_SESSION = "CHAT_SESSION"
    EMAIL_WORK_ITEM = "EMAIL_WORK_ITEM"
    KNOWLEDGE_DOCUMENT = "KNOWLEDGE_DOCUMENT"


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"
    __table_args__ = (
        CheckConstraint("chat_days > 0", name="ck_retention_policies_chat_days_positive"),
        CheckConstraint("email_days > 0", name="ck_retention_policies_email_days_positive"),
        CheckConstraint("audit_days > 0", name="ck_retention_policies_audit_days_positive"),
        CheckConstraint("version > 0", name="ck_retention_policies_version_positive"),
        UniqueConstraint("organization_id", name="uq_retention_policies_organization"),
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
    chat_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default=text("90")
    )
    email_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default=text("90")
    )
    audit_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default=text("365")
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

    @classmethod
    def default(cls, *, organization_id: UUID) -> "RetentionPolicy":
        return cls.configured(
            organization_id=organization_id,
            chat_days=90,
            email_days=90,
            audit_days=365,
        )

    @classmethod
    def configured(
        cls,
        *,
        organization_id: UUID,
        chat_days: int,
        email_days: int,
        audit_days: int,
    ) -> "RetentionPolicy":
        if min(chat_days, email_days, audit_days) <= 0:
            raise ValueError("retention periods must be positive")
        return cls(
            organization_id=organization_id,
            chat_days=chat_days,
            email_days=email_days,
            audit_days=audit_days,
            version=1,
        )


class ErasureRequest(Base):
    __tablename__ = "erasure_requests"
    __table_args__ = (
        CheckConstraint("replay_generation >= 0", name="ck_erasure_replay_generation_nonnegative"),
        Index(
            "ix_erasure_requests_subject",
            "organization_id",
            "subject_key_hash",
            "scope",
        ),
        Index("ix_erasure_requests_replay", "status", "replay_generation", "requested_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subject_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[ErasureScope] = mapped_column(
        Enum(ErasureScope, name="erasure_scope"), nullable=False
    )
    status: Mapped[ErasureStatus] = mapped_column(
        Enum(ErasureStatus, name="erasure_status"),
        nullable=False,
        default=ErasureStatus.PENDING,
        server_default=text("'PENDING'::erasure_status"),
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replay_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    verification_counts: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ErasureTarget(Base):
    __tablename__ = "erasure_targets"
    __table_args__ = (
        UniqueConstraint(
            "request_id", "target_type", "target_id", name="uq_erasure_targets_identity"
        ),
        Index("ix_erasure_targets_request", "request_id", "target_type"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("erasure_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_type: Mapped[ErasureTargetType] = mapped_column(
        Enum(ErasureTargetType, name="erasure_target_type"), nullable=False
    )
    target_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
