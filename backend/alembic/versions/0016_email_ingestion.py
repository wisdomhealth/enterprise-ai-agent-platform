"""Add durable Gmail ingestion, classification, and initial drafts.

Revision ID: 0016_email_ingestion
Revises: 0015_support_handoff_lifecycles
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_email_ingestion"
down_revision: str | None = "0015_support_handoff_lifecycles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE email_state AS ENUM ("
        "'INGESTED', 'DRAFTING', 'DRAFT_RETRY_WAIT', 'AWAITING_REVIEW', "
        "'APPROVED', 'SEND_PENDING', 'SENDING', 'SENT', 'REJECTED', "
        "'SEND_RETRY_WAIT', 'DELIVERY_UNKNOWN', 'FAILED_TERMINAL')"
    )
    op.execute(
        "CREATE TYPE email_action AS ENUM ("
        "'START_DRAFT', 'CLASSIFICATION_FAILED', 'CLASSIFIED_NO_DRAFT', "
        "'DRAFT_READY', 'DRAFT_FAILED', 'RETRY_DRAFT', 'APPROVE', 'REJECT', 'SEND')"
    )
    op.execute(
        "CREATE TYPE email_category AS ENUM ('ACTION_REQUIRED', 'INFORMATIONAL', 'SPAM', 'UNKNOWN')"
    )
    op.execute("CREATE TYPE email_priority AS ENUM ('HIGH', 'NORMAL', 'LOW')")

    op.create_table(
        "email_work_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=512), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=512), nullable=False),
        sa.Column("gmail_history_id", sa.String(length=128), nullable=True),
        sa.Column("sender", sa.String(length=1024), nullable=False),
        sa.Column("recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_content_ref", sa.String(length=1024), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(name="email_state", create_type=False),
            server_default=sa.text("'INGESTED'::email_state"),
            nullable=False,
        ),
        sa.Column(
            "category", postgresql.ENUM(name="email_category", create_type=False), nullable=True
        ),
        sa.Column(
            "priority", postgresql.ENUM(name="email_priority", create_type=False), nullable=True
        ),
        sa.Column("reply_required", sa.Boolean(), nullable=True),
        sa.Column(
            "classification_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("draft_body", sa.Text(), nullable=True),
        sa.Column(
            "draft_citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "draft_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=150), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_email_work_items_version_positive"),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "gmail_message_id", name="uq_email_work_items_org_message"
        ),
    )
    op.create_index(
        "ix_email_work_items_queue",
        "email_work_items",
        ["organization_id", "state", "received_at"],
    )
    op.create_index(
        "ix_email_work_items_connector",
        "email_work_items",
        ["connector_id", "received_at"],
    )

    op.create_table(
        "email_state_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "from_state", postgresql.ENUM(name="email_state", create_type=False), nullable=False
        ),
        sa.Column(
            "to_state", postgresql.ENUM(name="email_state", create_type=False), nullable=False
        ),
        sa.Column(
            "action", postgresql.ENUM(name="email_action", create_type=False), nullable=False
        ),
        sa.Column("reason_code", sa.String(length=150), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["staff_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["job_intents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_item_id"], ["email_work_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_state_history_item",
        "email_state_history",
        ["work_item_id", "created_at"],
    )

    op.create_table(
        "email_sync_states",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("history_id", sa.String(length=128), nullable=True),
        sa.Column("pending_page_token", sa.String(length=1024), nullable=True),
        sa.Column("last_error_code", sa.String(length=150), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connector_id", name="uq_email_sync_states_connector"),
    )
    op.create_index("ix_email_sync_states_organization", "email_sync_states", ["organization_id"])

    op.create_table(
        "email_evaluation_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("dataset_version", sa.String(length=200), nullable=False),
        sa.Column("dataset_kind", sa.String(length=32), nullable=False),
        sa.Column("dataset_digest", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=200), nullable=False),
        sa.Column("macro_f1", sa.Float(), nullable=False),
        sa.Column("structured_output_success", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("macro_f1 >= 0 AND macro_f1 <= 1", name="ck_email_eval_macro_f1"),
        sa.CheckConstraint(
            "structured_output_success >= 0 AND structured_output_success <= 1",
            name="ck_email_eval_structured_success",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_evaluation_runs_created", "email_evaluation_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_email_evaluation_runs_created", table_name="email_evaluation_runs")
    op.drop_table("email_evaluation_runs")
    op.drop_index("ix_email_sync_states_organization", table_name="email_sync_states")
    op.drop_table("email_sync_states")
    op.drop_index("ix_email_state_history_item", table_name="email_state_history")
    op.drop_table("email_state_history")
    op.drop_index("ix_email_work_items_connector", table_name="email_work_items")
    op.drop_index("ix_email_work_items_queue", table_name="email_work_items")
    op.drop_table("email_work_items")
    op.execute("DROP TYPE email_priority")
    op.execute("DROP TYPE email_category")
    op.execute("DROP TYPE email_action")
    op.execute("DROP TYPE email_state")
