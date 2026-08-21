"""Add organization-scoped knowledge bases and readonly Drive sources."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_knowledge_sources"
down_revision: str | None = "0007_connectors_and_encrypted_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

drive_source_status = postgresql.ENUM("ACTIVE", "ERROR", "DISABLED", name="drive_source_status")


def upgrade() -> None:
    bind = op.get_bind()
    drive_source_status.create(bind, checkfirst=True)
    op.create_table(
        "knowledge_bases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("default_language", sa.String(length=16), server_default=sa.text("'en'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_knowledge_bases_organization"),
    )
    op.create_table(
        "drive_sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_folder_id", sa.String(length=512), nullable=False),
        sa.Column("include_descendants", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "allowed_descendant_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("sync_cursor", sa.String(length=1024), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="drive_source_status", create_type=False),
            server_default=sa.text("'ACTIVE'::drive_source_status"),
            nullable=False,
        ),
        sa.Column("connection_identity", sa.String(length=320), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("knowledge_base_id", name="uq_drive_sources_knowledge_base"),
    )
    op.create_index("ix_drive_sources_organization", "drive_sources", ["organization_id"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE knowledge_bases, drive_sources TO platform_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE knowledge_bases, drive_sources FROM platform_app")
    op.drop_index("ix_drive_sources_organization", table_name="drive_sources")
    op.drop_table("drive_sources")
    op.drop_table("knowledge_bases")
    drive_source_status.drop(op.get_bind(), checkfirst=True)
