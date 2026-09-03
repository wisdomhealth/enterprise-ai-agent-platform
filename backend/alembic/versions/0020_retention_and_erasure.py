"""Add configurable retention and a persistent minimal erasure ledger.

Revision ID: 0020_retention_and_erasure
Revises: 0019_email_delivery
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020_retention_and_erasure"
down_revision: str | None = "0019_email_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE erasure_scope AS ENUM ('CUSTOMER', 'KNOWLEDGE_DOCUMENT')")
    op.execute("CREATE TYPE erasure_status AS ENUM ('PENDING', 'APPLIED', 'FAILED')")
    op.execute(
        "CREATE TYPE erasure_target_type AS ENUM "
        "('CHAT_SESSION', 'EMAIL_WORK_ITEM', 'KNOWLEDGE_DOCUMENT')"
    )
    op.create_table(
        "retention_policies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("email_days", sa.Integer(), server_default=sa.text("90"), nullable=False),
        sa.Column("audit_days", sa.Integer(), server_default=sa.text("365"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("chat_days > 0", name="ck_retention_policies_chat_days_positive"),
        sa.CheckConstraint("email_days > 0", name="ck_retention_policies_email_days_positive"),
        sa.CheckConstraint("audit_days > 0", name="ck_retention_policies_audit_days_positive"),
        sa.CheckConstraint("version > 0", name="ck_retention_policies_version_positive"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_retention_policies_organization"),
    )
    op.create_table(
        "erasure_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scope", postgresql.ENUM(name="erasure_scope", create_type=False), nullable=False
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="erasure_status", create_type=False),
            server_default=sa.text("'PENDING'::erasure_status"),
            nullable=False,
        ),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "verification_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.CheckConstraint(
            "replay_generation >= 0", name="ck_erasure_replay_generation_nonnegative"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by_id"], ["staff_users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_erasure_requests_subject",
        "erasure_requests",
        ["organization_id", "subject_key_hash", "scope"],
    )
    op.create_index(
        "ix_erasure_requests_replay",
        "erasure_requests",
        ["status", "replay_generation", "requested_at"],
    )
    op.create_table(
        "erasure_targets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "target_type",
            postgresql.ENUM(name="erasure_target_type", create_type=False),
            nullable=False,
        ),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["request_id"], ["erasure_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id", "target_type", "target_id", name="uq_erasure_targets_identity"
        ),
    )
    op.create_index("ix_erasure_targets_request", "erasure_targets", ["request_id", "target_type"])

    op.execute(
        "INSERT INTO retention_policies (organization_id) "
        "SELECT id FROM organizations ON CONFLICT (organization_id) DO NOTHING"
    )
    op.execute(
        "INSERT INTO resource_grants "
        "(organization_id, subject_id, resource_type, resource_id, actions) "
        "SELECT u.organization_id, u.id, 'retention', p.id, "
        "ARRAY['retention.read','retention.write','retention.erase']::varchar[] "
        "FROM staff_users u JOIN retention_policies p ON p.organization_id = u.organization_id "
        "WHERE u.role = 'ADMIN' AND u.status = 'ACTIVE' "
        "ON CONFLICT (organization_id, subject_id, resource_type, resource_id) "
        "DO UPDATE SET actions = EXCLUDED.actions"
    )
    op.execute(
        """
        CREATE FUNCTION create_default_retention_policy() RETURNS trigger AS $$
        BEGIN
            INSERT INTO retention_policies (organization_id)
            VALUES (NEW.id) ON CONFLICT (organization_id) DO NOTHING;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER organizations_default_retention_policy "
        "AFTER INSERT ON organizations "
        "FOR EACH ROW EXECUTE FUNCTION create_default_retention_policy()"
    )
    op.execute(
        """
        CREATE FUNCTION reconcile_admin_retention_grant() RETURNS trigger AS $$
        DECLARE policy_id uuid;
        BEGIN
            SELECT id INTO policy_id FROM retention_policies
            WHERE organization_id = NEW.organization_id;
            IF NEW.role = 'ADMIN' AND NEW.status = 'ACTIVE' THEN
                INSERT INTO resource_grants
                    (organization_id, subject_id, resource_type, resource_id, actions)
                VALUES (
                    NEW.organization_id, NEW.id, 'retention', policy_id,
                    ARRAY['retention.read','retention.write','retention.erase']::varchar[]
                )
                ON CONFLICT (organization_id, subject_id, resource_type, resource_id)
                DO UPDATE SET actions = EXCLUDED.actions;
            ELSE
                DELETE FROM resource_grants
                WHERE organization_id = NEW.organization_id
                  AND subject_id = NEW.id
                  AND resource_type = 'retention';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER staff_users_retention_grant "
        "AFTER INSERT OR UPDATE OF role, status ON staff_users "
        "FOR EACH ROW EXECUTE FUNCTION reconcile_admin_retention_grant()"
    )

    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE retention_policies, erasure_requests, "
        "erasure_targets FROM platform_app"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE retention_policies TO platform_app")
    op.execute(
        "GRANT UPDATE (chat_days, email_days, audit_days, version, updated_at) "
        "ON retention_policies TO platform_app"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE erasure_requests TO platform_app")
    op.execute(
        "GRANT UPDATE (status, applied_at, replay_generation, verification_counts, "
        "last_error_code) ON erasure_requests TO platform_app"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE erasure_targets TO platform_app")
    op.execute(
        "GRANT SELECT ON TABLE chat_sessions, chat_messages, support_handoffs TO platform_app"
    )
    op.execute(
        "GRANT UPDATE (customer_name, customer_email, updated_at) ON chat_sessions TO platform_app"
    )
    op.execute("GRANT UPDATE (body) ON chat_messages TO platform_app")
    op.execute("GRANT UPDATE (snapshot, updated_at) ON support_handoffs TO platform_app")
    op.execute(
        "GRANT UPDATE (body, to_recipients, cc_recipients, subject, reviewer_instruction, "
        "retrieval_config, citations) ON email_draft_versions TO platform_app"
    )
    op.execute("GRANT DELETE ON audit_events TO platform_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON audit_events FROM platform_app")
    op.execute(
        "REVOKE UPDATE (body, to_recipients, cc_recipients, subject, reviewer_instruction, "
        "retrieval_config, citations) ON email_draft_versions FROM platform_app"
    )
    op.execute("REVOKE UPDATE (snapshot, updated_at) ON support_handoffs FROM platform_app")
    op.execute("REVOKE UPDATE (body) ON chat_messages FROM platform_app")
    op.execute(
        "REVOKE UPDATE (customer_name, customer_email, updated_at) "
        "ON chat_sessions FROM platform_app"
    )
    op.execute(
        "REVOKE SELECT ON TABLE chat_sessions, chat_messages, support_handoffs FROM platform_app"
    )
    op.execute("DROP TRIGGER staff_users_retention_grant ON staff_users")
    op.execute("DROP FUNCTION reconcile_admin_retention_grant()")
    op.execute("DROP TRIGGER organizations_default_retention_policy ON organizations")
    op.execute("DROP FUNCTION create_default_retention_policy()")
    op.execute("DELETE FROM resource_grants WHERE resource_type = 'retention'")
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE retention_policies, erasure_requests, "
        "erasure_targets FROM platform_app"
    )
    op.drop_index("ix_erasure_targets_request", table_name="erasure_targets")
    op.drop_table("erasure_targets")
    op.drop_index(
        "ix_erasure_requests_subject",
        table_name="erasure_requests",
        if_exists=True,
    )
    op.drop_index("ix_erasure_requests_replay", table_name="erasure_requests")
    op.drop_table("erasure_requests")
    op.drop_table("retention_policies")
    op.execute("DROP TYPE erasure_target_type")
    op.execute("DROP TYPE erasure_status")
    op.execute("DROP TYPE erasure_scope")
