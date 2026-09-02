"""Add immutable email draft versions and reviewer approvals.

Revision ID: 0018_email_review
Revises: 0017_email_ingestion_hardening
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018_email_review"
down_revision: str | None = "0017_email_ingestion_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_draft_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("to_recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "cc_recipients",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.String(length=512), nullable=False),
        sa.Column("reviewer_instruction", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=200), nullable=False),
        sa.Column(
            "retrieval_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "creator_type", sa.String(length=16), server_default=sa.text("'SYSTEM'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_email_draft_versions_version_positive"),
        sa.CheckConstraint(
            "creator_type IN ('SYSTEM', 'STAFF')", name="ck_email_draft_versions_creator_type"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"], ["email_work_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_item_id", "version", name="uq_email_draft_versions_item_version"),
    )
    op.create_index(
        "ix_email_draft_versions_item_created",
        "email_draft_versions",
        ["work_item_id", "created_at"],
    )

    op.create_table(
        "email_approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= approved_at",
            name="ck_email_approvals_invalidation_order",
        ),
        sa.ForeignKeyConstraint(
            ["draft_version_id"], ["email_draft_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["reviewer_id"], ["staff_users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["work_item_id"], ["email_work_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_version_id", name="uq_email_approvals_draft_version"),
    )
    op.create_index(
        "ix_email_approvals_item_active",
        "email_approvals",
        ["work_item_id", "invalidated_at"],
    )

    op.add_column(
        "email_work_items",
        sa.Column("current_draft_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        INSERT INTO email_draft_versions (
            id, work_item_id, organization_id, version, body, to_recipients,
            cc_recipients, subject, thread_id, reviewer_instruction, model,
            prompt_version, retrieval_config, citations, created_by_id,
            creator_type, created_at
        )
        SELECT
            gen_random_uuid(), id, organization_id, 1, draft_body,
            jsonb_build_array(sender), '[]'::jsonb,
            CASE WHEN subject ILIKE 're:%' THEN subject ELSE 'Re: ' || subject END,
            gmail_thread_id, NULL,
            COALESCE(NULLIF(draft_provenance->>'model', ''), 'legacy-import'),
            COALESCE(NULLIF(draft_provenance->>'prompt_version', ''), 'legacy-import'),
            draft_provenance - 'model' - 'prompt_version',
            draft_citations, NULL, 'SYSTEM', updated_at
        FROM email_work_items
        WHERE draft_body IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE email_work_items AS item
        SET current_draft_id = draft.id
        FROM email_draft_versions AS draft
        WHERE draft.work_item_id = item.id AND draft.version = 1
        """
    )
    op.create_foreign_key(
        "fk_email_work_items_current_draft",
        "email_work_items",
        "email_draft_versions",
        ["current_draft_id"],
        ["id"],
        ondelete="RESTRICT",
        use_alter=True,
    )
    op.create_index(
        "ix_email_work_items_current_draft", "email_work_items", ["current_draft_id"]
    )

    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE email_draft_versions, email_approvals FROM platform_app"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE email_draft_versions TO platform_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE email_approvals TO platform_app")


def downgrade() -> None:
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE email_draft_versions, email_approvals FROM platform_app"
    )
    op.drop_index("ix_email_work_items_current_draft", table_name="email_work_items")
    op.drop_constraint(
        "fk_email_work_items_current_draft", "email_work_items", type_="foreignkey"
    )
    op.drop_column("email_work_items", "current_draft_id")
    op.drop_index("ix_email_approvals_item_active", table_name="email_approvals")
    op.drop_table("email_approvals")
    op.drop_index("ix_email_draft_versions_item_created", table_name="email_draft_versions")
    op.drop_table("email_draft_versions")
