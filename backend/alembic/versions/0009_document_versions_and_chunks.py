"""Add versioned documents and deterministic parsed chunks."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_document_versions_and_chunks"
down_revision: str | None = "0008_knowledge_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

document_version_state = postgresql.ENUM(
    "PROCESSING",
    "RETRIEVABLE",
    "FAILED",
    "REVOKED",
    "DELETED",
    name="document_version_state",
)


def upgrade() -> None:
    bind = op.get_bind()
    document_version_state.create(bind, checkfirst=True)
    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["drive_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
    )
    op.create_table(
        "document_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(name="document_version_state", create_type=False),
            server_default=sa.text("'PROCESSING'::document_version_state"),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=150), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "content_sha256", name="uq_document_versions_content"),
    )
    op.create_foreign_key(
        "fk_documents_current_version",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=1024), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id", "ordinal", name="uq_document_chunks_version_ordinal"
        ),
    )
    op.create_index("ix_documents_knowledge_base", "documents", ["knowledge_base_id"])
    op.create_index(
        "ix_document_versions_document_state", "document_versions", ["document_id", "state"]
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE documents, document_versions, "
        "document_chunks TO platform_app"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE documents, document_versions, "
        "document_chunks FROM platform_app"
    )
    op.drop_index("ix_document_versions_document_state", table_name="document_versions")
    op.drop_index("ix_documents_knowledge_base", table_name="documents")
    op.drop_table("document_chunks")
    op.drop_constraint("fk_documents_current_version", "documents", type_="foreignkey")
    op.drop_table("document_versions")
    op.drop_table("documents")
    document_version_state.drop(op.get_bind(), checkfirst=True)
