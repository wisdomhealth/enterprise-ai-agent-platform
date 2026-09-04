"""Add append-only audit, transactional outbox, jobs, and idempotency."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_audit_outbox_jobs"
down_revision: str | None = "0004_grant_subject_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

job_state = postgresql.ENUM(
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "RECONCILIATION",
    name="job_state",
)
error_class = postgresql.ENUM(
    "RETRYABLE",
    "NON_RETRYABLE",
    "AMBIGUOUS",
    "SECURITY",
    name="error_class",
)
idempotency_state = postgresql.ENUM(
    "IN_PROGRESS",
    "COMPLETED",
    name="idempotency_state",
)


def upgrade() -> None:
    bind = op.get_bind()
    job_state.create(bind, checkfirst=True)
    error_class.create(bind, checkfirst=True)
    idempotency_state.create(bind, checkfirst=True)

    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("object_type", sa.String(length=100), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=50), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_scope_occurred_at",
        "audit_events",
        ["organization_id", "occurred_at"],
    )

    op.create_table(
        "outbox_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column("event_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publish_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0", name="ck_outbox_events_publish_attempts_nonnegative"
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["occurred_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "processed_events",
        sa.Column("consumer_name", sa.String(length=150), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer_name", "event_id"),
    )

    op.create_table(
        "job_intents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=150), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(name="job_state", create_type=False),
            server_default=sa.text("'PENDING'::job_state"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=150), nullable=True),
        sa.Column(
            "error_class",
            postgresql.ENUM(name="error_class", create_type=False),
            nullable=True,
        ),
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
        sa.CheckConstraint("attempts >= 0", name="ck_job_intents_attempts_nonnegative"),
        sa.CheckConstraint("version > 0", name="ck_job_intents_version_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "idempotency_key", name="uq_job_intents_kind_key"),
    )
    op.create_index(
        "ix_job_intents_claimable",
        "job_intents",
        ["state", "next_attempt_at", "lease_expires_at"],
    )

    op.create_table(
        "idempotency_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=150), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(name="idempotency_state", create_type=False),
            server_default=sa.text("'IN_PROGRESS'::idempotency_state"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_id",
            "actor_id",
            "operation",
            "object_id",
            "key",
            name="uq_idempotency_records_binding",
        ),
        sa.UniqueConstraint(
            "scope_id", "actor_id", "key", name="uq_idempotency_records_actor_key"
        ),
    )

    op.execute(
        """
        DO $role$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_app') THEN
                CREATE ROLE platform_app LOGIN;
            END IF;
        END
        $role$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO platform_app")
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON TABLE organizations, staff_users, staff_sessions, resource_grants,
                 outbox_events, processed_events, job_intents, idempotency_records
        TO platform_app
        """
    )
    op.execute("REVOKE ALL PRIVILEGES ON audit_events FROM platform_app")
    op.execute("GRANT SELECT, INSERT ON audit_events TO platform_app")
    op.execute("REVOKE ALL PRIVILEGES ON alembic_version FROM platform_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT, INSERT ON audit_events FROM platform_app")
    op.drop_table("idempotency_records")
    op.drop_index("ix_job_intents_claimable", table_name="job_intents")
    op.drop_table("job_intents")
    op.drop_table("processed_events")
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_audit_events_scope_occurred_at", table_name="audit_events")
    op.drop_table("audit_events")

    bind = op.get_bind()
    idempotency_state.drop(bind, checkfirst=True)
    error_class.drop(bind, checkfirst=True)
    job_state.drop(bind, checkfirst=True)
