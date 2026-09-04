"""Persist subject-to-resource action grants."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_resource_grants"
down_revision: str | None = "0002_identity_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_grants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actions", postgresql.ARRAY(sa.String(length=100)), nullable=False),
        sa.CheckConstraint(
            "cardinality(actions) > 0",
            name="ck_resource_grants_actions_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["staff_users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "subject_id",
            "resource_type",
            "resource_id",
            name="uq_resource_grants_subject_resource",
        ),
    )
    op.create_index(
        "ix_resource_grants_subject_lookup",
        "resource_grants",
        ["organization_id", "subject_id", "resource_type", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_resource_grants_subject_lookup", table_name="resource_grants")
    op.drop_table("resource_grants")
