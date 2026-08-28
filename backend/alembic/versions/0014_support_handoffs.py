"""Add durable support handoffs.

Revision ID: 0014_support_handoffs
Revises: 0013_chat_sessions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_support_handoffs"
down_revision: str | None = "0013_chat_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE handoff_trigger AS ENUM ('CUSTOMER_REQUEST', 'LOW_CONFIDENCE', 'REPEATED_FAILURE', 'SENSITIVE_TOPIC', 'SYSTEM_ERROR')")
    op.execute("CREATE TYPE sensitive_topic AS ENUM ('ACCOUNT_SECURITY', 'PAYMENT_DATA', 'LEGAL_THREAT', 'SAFETY', 'PRIVACY_REQUEST')")
    op.create_table(
        "support_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", postgresql.ENUM(name="conversation_state", create_type=False), nullable=False),
        sa.Column("trigger", postgresql.ENUM(name="handoff_trigger", create_type=False), nullable=False),
        sa.Column("sensitive_topic", postgresql.ENUM(name="sensitive_topic", create_type=False), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_customer_sequence", sa.Integer(), nullable=False),
        sa.Column("assigned_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["staff_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_support_handoffs_session"),
    )
    op.create_index("ix_support_handoffs_queue", "support_handoffs", ["organization_id", "state", "created_at"])
    op.create_index("ix_support_handoffs_assignee", "support_handoffs", ["assigned_user_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_support_handoffs_assignee", table_name="support_handoffs")
    op.drop_index("ix_support_handoffs_queue", table_name="support_handoffs")
    op.drop_table("support_handoffs")
    op.execute("DROP TYPE sensitive_topic")
    op.execute("DROP TYPE handoff_trigger")
