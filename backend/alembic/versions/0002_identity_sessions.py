"""Add invitation binding and server-side staff sessions."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_identity_sessions"
down_revision: str | None = "0001_platform_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("staff_users", "oidc_subject", existing_type=sa.String(255), nullable=True)
    op.create_table(
        "staff_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["staff_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staff_sessions_user_id", "staff_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_staff_sessions_user_id", table_name="staff_sessions")
    op.drop_table("staff_sessions")
    op.alter_column("staff_users", "oidc_subject", existing_type=sa.String(255), nullable=False)
