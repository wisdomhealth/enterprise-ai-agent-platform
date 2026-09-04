"""Add anonymous public chat sessions and credentials."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_chat_sessions"
down_revision: str | None = "0012_rag_evaluation_case_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "public_key",
            sa.String(length=64),
            server_default=sa.text("replace(gen_random_uuid()::text, '-', '')"),
            nullable=False,
        ),
    )
    op.create_unique_constraint("uq_knowledge_bases_public_key", "knowledge_bases", ["public_key"])
    op.execute("CREATE TYPE conversation_state AS ENUM ('AI_ACTIVE', 'HANDOFF_REQUESTED', 'QUEUED', 'HUMAN_ACTIVE', 'RESOLVED')")
    op.execute("CREATE TYPE chat_actor AS ENUM ('CUSTOMER', 'AI', 'STAFF', 'SYSTEM')")
    op.execute("CREATE TYPE chat_message_status AS ENUM ('PERSISTED')")
    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", postgresql.ENUM(name="conversation_state", create_type=False), server_default=sa.text("'AI_ACTIVE'::conversation_state"), nullable=False),
        sa.Column("customer_name", sa.String(length=200), nullable=True),
        sa.Column("customer_email", sa.String(length=320), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_sessions_organization", "chat_sessions", ["organization_id"])
    op.create_index("ix_chat_sessions_knowledge_base", "chat_sessions", ["knowledge_base_id"])
    op.create_table(
        "chat_session_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_chat_session_credentials_token_hash"),
    )
    op.create_index("ix_chat_session_credentials_session", "chat_session_credentials", ["session_id"])
    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("actor", postgresql.ENUM(name="chat_actor", create_type=False), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", postgresql.ENUM(name="chat_message_status", create_type=False), server_default=sa.text("'PERSISTED'::chat_message_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sequence", name="uq_chat_messages_session_sequence"),
    )
    op.create_index("ix_chat_messages_session_sequence", "chat_messages", ["session_id", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_sequence", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_session_credentials_session", table_name="chat_session_credentials")
    op.drop_table("chat_session_credentials")
    op.drop_index("ix_chat_sessions_knowledge_base", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_organization", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.execute("DROP TYPE chat_message_status")
    op.execute("DROP TYPE chat_actor")
    op.execute("DROP TYPE conversation_state")
    op.drop_constraint("uq_knowledge_bases_public_key", "knowledge_bases", type_="unique")
    op.drop_column("knowledge_bases", "public_key")
