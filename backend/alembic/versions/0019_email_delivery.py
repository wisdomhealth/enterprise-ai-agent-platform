"""Add Gmail delivery intents, attempts, and successful outcomes.

Revision ID: 0019_email_delivery
Revises: 0018_email_review
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019_email_delivery"
down_revision: str | None = "0018_email_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for action in (
        "QUEUE_SEND",
        "CLAIM_SEND",
        "SEND_SUCCEEDED",
        "SEND_FAILED",
        "RETRY_SEND",
        "DELIVERY_AMBIGUOUS",
        "RECONCILE_SENT",
        "RECONCILE_ABSENT",
    ):
        op.execute(f"ALTER TYPE email_action ADD VALUE IF NOT EXISTS '{action}'")

    op.create_table(
        "email_delivery_intents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_draft_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deterministic_message_id", sa.String(length=512), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(name="email_state", create_type=False),
            server_default=sa.text("'SEND_PENDING'::email_state"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=150), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version > 0", name="ck_email_delivery_intents_version_positive"),
        sa.ForeignKeyConstraint(["approval_id"], ["email_approvals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["approved_draft_version_id"], ["email_draft_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_intents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_item_id"], ["email_work_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "approved_draft_version_id", name="uq_email_delivery_intents_approved_draft"
        ),
        sa.UniqueConstraint(
            "deterministic_message_id", name="uq_email_delivery_intents_message_id"
        ),
        sa.UniqueConstraint("job_id", name="uq_email_delivery_intents_job"),
    )
    op.create_index(
        "ix_email_delivery_intents_queue",
        "email_delivery_intents",
        ["organization_id", "state", "created_at"],
    )

    op.create_table(
        "email_delivery_attempts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("delivery_intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "outcome", sa.String(length=32), server_default=sa.text("'IN_PROGRESS'"), nullable=False
        ),
        sa.Column("error_code", sa.String(length=150), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_email_delivery_attempts_number_positive"),
        sa.CheckConstraint(
            "outcome IN ('IN_PROGRESS', 'SENT', 'DEFINITIVE_FAILURE', 'UNKNOWN')",
            name="ck_email_delivery_attempts_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_intent_id"], ["email_delivery_intents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_intent_id", "attempt_number", name="uq_email_delivery_attempt_number"
        ),
    )
    op.create_index(
        "ix_email_delivery_attempts_intent",
        "email_delivery_attempts",
        ["delivery_intent_id", "started_at"],
    )

    op.create_table(
        "email_successful_deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("delivery_intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=512), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=512), nullable=False),
        sa.Column("reconciled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["delivery_intent_id"], ["email_delivery_intents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_intent_id", name="uq_email_success_delivery_intent"),
    )
    op.create_index(
        "ix_email_successful_deliveries_message",
        "email_successful_deliveries",
        ["gmail_message_id"],
    )

    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE email_delivery_intents, email_delivery_attempts, "
        "email_successful_deliveries FROM platform_app"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE email_delivery_intents, "
        "email_delivery_attempts TO platform_app"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE email_successful_deliveries TO platform_app")


def downgrade() -> None:
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE email_delivery_intents, email_delivery_attempts, "
        "email_successful_deliveries FROM platform_app"
    )
    op.drop_index(
        "ix_email_successful_deliveries_message", table_name="email_successful_deliveries"
    )
    op.drop_table("email_successful_deliveries")
    op.drop_index("ix_email_delivery_attempts_intent", table_name="email_delivery_attempts")
    op.drop_table("email_delivery_attempts")
    op.drop_index("ix_email_delivery_intents_queue", table_name="email_delivery_intents")
    op.drop_table("email_delivery_intents")
