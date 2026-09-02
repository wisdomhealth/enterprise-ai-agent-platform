from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.jobs.models import JobIntent


class EmailState(StrEnum):
    INGESTED = "INGESTED"
    DRAFTING = "DRAFTING"
    DRAFT_RETRY_WAIT = "DRAFT_RETRY_WAIT"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPROVED = "APPROVED"
    SEND_PENDING = "SEND_PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    REJECTED = "REJECTED"
    SEND_RETRY_WAIT = "SEND_RETRY_WAIT"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    FAILED_TERMINAL = "FAILED_TERMINAL"


class EmailAction(StrEnum):
    START_DRAFT = "START_DRAFT"
    CLASSIFICATION_FAILED = "CLASSIFICATION_FAILED"
    CLASSIFIED_NO_DRAFT = "CLASSIFIED_NO_DRAFT"
    DRAFT_READY = "DRAFT_READY"
    DRAFT_FAILED = "DRAFT_FAILED"
    RETRY_DRAFT = "RETRY_DRAFT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SEND = "SEND"


class EmailCategory(StrEnum):
    ACTION_REQUIRED = "ACTION_REQUIRED"
    INFORMATIONAL = "INFORMATIONAL"
    SPAM = "SPAM"
    UNKNOWN = "UNKNOWN"


class EmailPriority(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class EmailWorkItem(Base):
    __tablename__ = "email_work_items"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "gmail_message_id", name="uq_email_work_items_org_message"
        ),
        CheckConstraint("version > 0", name="ck_email_work_items_version_positive"),
        Index("ix_email_work_items_queue", "organization_id", "state", "received_at"),
        Index("ix_email_work_items_connector", "connector_id", "received_at"),
        Index("ix_email_work_items_current_draft", "current_draft_id"),
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
    connector_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("connectors.id", ondelete="RESTRICT"),
        nullable=False,
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gmail_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(512), nullable=False)
    gmail_history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender: Mapped[str] = mapped_column(String(1024), nullable=False)
    recipients: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_content_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[EmailState] = mapped_column(
        Enum(EmailState, name="email_state"),
        nullable=False,
        default=EmailState.INGESTED,
        server_default=text("'INGESTED'::email_state"),
    )
    category: Mapped[EmailCategory | None] = mapped_column(
        Enum(EmailCategory, name="email_category"), nullable=True
    )
    priority: Mapped[EmailPriority | None] = mapped_column(
        Enum(EmailPriority, name="email_priority"), nullable=True
    )
    reply_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    classification_provenance: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    draft_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_citations: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    draft_provenance: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    current_draft_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "email_draft_versions.id",
            name="fk_email_work_items_current_draft",
            ondelete="RESTRICT",
            use_alter=True,
        ),
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


class EmailDraftVersion(Base):
    __tablename__ = "email_draft_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_email_draft_versions_version_positive"),
        CheckConstraint(
            "creator_type IN ('SYSTEM', 'STAFF')", name="ck_email_draft_versions_creator_type"
        ),
        UniqueConstraint(
            "work_item_id", "version", name="uq_email_draft_versions_item_version"
        ),
        Index("ix_email_draft_versions_item_created", "work_item_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_work_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    to: Mapped[list[str]] = mapped_column("to_recipients", JSONB, nullable=False)
    cc: Mapped[list[str]] = mapped_column(
        "cc_recipients", JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(512), nullable=False)
    reviewer_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(200), nullable=False)
    retrieval_config: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    citations: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_by_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    creator_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SYSTEM", server_default=text("'SYSTEM'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailApproval(Base):
    __tablename__ = "email_approvals"
    __table_args__ = (
        CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= approved_at",
            name="ck_email_approvals_invalidation_order",
        ),
        UniqueConstraint("draft_version_id", name="uq_email_approvals_draft_version"),
        Index("ix_email_approvals_item_active", "work_item_id", "invalidated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_work_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    draft_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_draft_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reviewer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailStateHistory(Base):
    __tablename__ = "email_state_history"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('SYSTEM', 'STAFF')", name="ck_email_state_history_actor_type"
        ),
        Index("ix_email_state_history_item", "work_item_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_work_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_state: Mapped[EmailState] = mapped_column(
        Enum(EmailState, name="email_state", create_type=False), nullable=False
    )
    to_state: Mapped[EmailState] = mapped_column(
        Enum(EmailState, name="email_state", create_type=False), nullable=False
    )
    action: Mapped[EmailAction] = mapped_column(
        Enum(EmailAction, name="email_action"), nullable=False
    )
    reason_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    actor_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=True,
    )
    actor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SYSTEM", server_default=text("'SYSTEM'")
    )
    job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(JobIntent.id, ondelete="SET NULL"),
        nullable=True,
    )
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailSyncState(Base):
    __tablename__ = "email_sync_states"
    __table_args__ = (
        UniqueConstraint("connector_id", name="uq_email_sync_states_connector"),
        Index("ix_email_sync_states_organization", "organization_id"),
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
    connector_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("connectors.id", ondelete="CASCADE"),
        nullable=False,
    )
    history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_page_token: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EmailEvaluationRun(Base):
    __tablename__ = "email_evaluation_runs"
    __table_args__ = (
        CheckConstraint("macro_f1 >= 0 AND macro_f1 <= 1", name="ck_email_eval_macro_f1"),
        CheckConstraint(
            "structured_output_success >= 0 AND structured_output_success <= 1",
            name="ck_email_eval_structured_success",
        ),
        CheckConstraint(
            "category_macro_f1 IS NULL OR (category_macro_f1 >= 0 AND category_macro_f1 <= 1)",
            name="ck_email_eval_category_macro_f1",
        ),
        CheckConstraint(
            "priority_macro_f1 IS NULL OR (priority_macro_f1 >= 0 AND priority_macro_f1 <= 1)",
            name="ck_email_eval_priority_macro_f1",
        ),
        CheckConstraint(
            "reply_required_f1 IS NULL OR (reply_required_f1 >= 0 AND reply_required_f1 <= 1)",
            name="ck_email_eval_reply_required_f1",
        ),
        CheckConstraint(
            "exact_match_rate IS NULL OR (exact_match_rate >= 0 AND exact_match_rate <= 1)",
            name="ck_email_eval_exact_match_rate",
        ),
        CheckConstraint(
            "metrics_version <> 'email-classification-v2' OR "
            "(category_macro_f1 IS NOT NULL AND priority_macro_f1 IS NOT NULL AND "
            "reply_required_f1 IS NOT NULL AND exact_match_rate IS NOT NULL)",
            name="ck_email_eval_complete_v2_metrics",
        ),
        Index("ix_email_evaluation_runs_created", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    dataset_version: Mapped[str] = mapped_column(String(200), nullable=False)
    dataset_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(200), nullable=False)
    metrics_version: Mapped[str] = mapped_column(
        String(100), nullable=False, server_default=text("'email-category-only-v1'")
    )
    macro_f1: Mapped[float] = mapped_column(Float, nullable=False)
    category_macro_f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_macro_f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    reply_required_f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    exact_match_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    structured_output_success: Mapped[float] = mapped_column(Float, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
