"""Add Gmail push state, application access, and bounded draft retry metadata.

Revision ID: 0022_gmail_push_access_draft_reliability
Revises: 0021_webhook_subscriptions
"""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022_gmail_push_access_draft_reliability"
down_revision: str | None = "0021_webhook_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _application_resource_id(organization_id: UUID, capability: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"application-access:{organization_id}:{capability}",
    )


def upgrade() -> None:
    op.execute(
        "CREATE TYPE gmail_watch_status AS ENUM "
        "('STOPPED', 'STARTING', 'ACTIVE', 'STOPPING', 'ERROR')"
    )
    op.execute(
        "CREATE TYPE gmail_sync_status AS ENUM "
        "('IDLE', 'RUNNING', 'PAUSED_ERROR', 'RECOVERY_REQUIRED', "
        "'RECOVERY_RUNNING', 'RECOVERY_TRUNCATED')"
    )
    op.alter_column("email_sync_states", "history_id", new_column_name="processed_history_id")
    for name, column_type in (
        ("mailbox_address", sa.String(length=320)),
        ("pending_history_id", sa.String(length=128)),
        ("sync_start_history_id", sa.String(length=128)),
        ("watch_topic_name", sa.String(length=1024)),
        ("watch_history_id", sa.String(length=128)),
        ("watch_last_error_code", sa.String(length=150)),
        ("recovery_baseline_history_id", sa.String(length=128)),
        ("recovery_page_token", sa.String(length=1024)),
    ):
        op.add_column("email_sync_states", sa.Column(name, column_type, nullable=True))
    op.add_column("email_sync_states", sa.Column("page_token_generation", sa.Integer()))
    op.add_column(
        "email_sync_states", sa.Column("active_sync_job_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "email_sync_states",
        sa.Column("sync_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "email_sync_states",
        sa.Column(
            "sync_status",
            postgresql.ENUM(name="gmail_sync_status", create_type=False),
            server_default=sa.text("'IDLE'::gmail_sync_status"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_sync_states",
        sa.Column(
            "auto_sync_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_sync_states",
        sa.Column(
            "watch_status",
            postgresql.ENUM(name="gmail_watch_status", create_type=False),
            server_default=sa.text("'STOPPED'::gmail_watch_status"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_sync_states",
        sa.Column(
            "watch_operation_generation", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    for name in (
        "watch_expiration_at",
        "last_watch_success_at",
        "last_notification_at",
        "recovery_window_started_at",
        "recovery_started_at",
    ):
        op.add_column("email_sync_states", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column(
        "email_sync_states", sa.Column("recovery_batch_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "email_sync_states",
        sa.Column(
            "recovery_processed_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "email_sync_states",
        sa.Column(
            "recovery_message_limit", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "email_sync_states",
        sa.Column(
            "recovery_has_more",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_sync_states",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_unique_constraint(
        "uq_email_sync_states_mailbox", "email_sync_states", ["mailbox_address"]
    )
    op.create_foreign_key(
        "fk_email_sync_states_active_job",
        "email_sync_states",
        "job_intents",
        ["active_sync_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "email_work_items", sa.Column("active_draft_job_id", postgresql.UUID(as_uuid=True))
    )
    op.create_foreign_key(
        "fk_email_work_items_active_draft_job",
        "email_work_items",
        "job_intents",
        ["active_draft_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for name in ("attempt_cycle", "cycle_start_attempts", "cycle_attempt_limit"):
        op.add_column("job_intents", sa.Column(name, sa.Integer()))

    connection = op.get_bind()
    draft_rows = connection.execute(
        sa.text(
            """
            SELECT w.id, array_agg(j.id ORDER BY j.created_at, j.id) AS job_ids
            FROM email_work_items AS w
            JOIN job_intents AS j
              ON j.kind = 'email.draft'
             AND j.payload->>'work_item_id' = w.id::text
             AND j.state IN ('PENDING', 'RUNNING', 'RECONCILIATION')
            WHERE w.state IN ('DRAFTING', 'DRAFT_RETRY_WAIT')
            GROUP BY w.id
            """
        )
    ).all()
    for work_item_id, job_ids in draft_rows:
        if len(job_ids) == 1:
            connection.execute(
                sa.text(
                    "UPDATE email_work_items SET active_draft_job_id = :job_id "
                    "WHERE id = :work_item_id"
                ),
                {"job_id": job_ids[0], "work_item_id": work_item_id},
            )
            connection.execute(
                sa.text(
                    """
                    UPDATE job_intents
                    SET attempt_cycle = 1,
                        cycle_start_attempts = attempts,
                        cycle_attempt_limit = 3
                    WHERE id = :job_id
                    """
                ),
                {"job_id": job_ids[0]},
            )
            continue
        connection.execute(
            sa.text(
                """
                UPDATE email_work_items
                SET state = 'FAILED_TERMINAL',
                    last_error_code = 'EMAIL_DRAFT_CANONICAL_JOB_AMBIGUOUS',
                    active_draft_job_id = NULL,
                    version = version + 1
                WHERE id = :work_item_id
                """
            ),
            {"work_item_id": work_item_id},
        )
        connection.execute(
            sa.text(
                """
                UPDATE job_intents
                SET state = 'FAILED',
                    error_class = 'NON_RETRYABLE',
                    last_error_code = 'EMAIL_DRAFT_CANONICAL_JOB_AMBIGUOUS',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    next_attempt_at = NULL,
                    version = version + 1
                WHERE id = ANY(:job_ids)
                  AND state IN ('PENDING', 'RUNNING', 'RECONCILIATION')
                """
            ),
            {"job_ids": job_ids},
        )

    op.create_table(
        "gmail_push_receipts",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_name", sa.String(length=1024), nullable=False),
        sa.Column("pubsub_message_id", sa.String(length=255), nullable=False),
        sa.Column("notification_history_id", sa.String(length=128), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_name", "pubsub_message_id",
            name="uq_gmail_push_receipts_subscription_message",
        ),
    )
    op.create_index(
        "ix_gmail_push_receipts_expiry", "gmail_push_receipts", ["expires_at"]
    )

    rows = connection.execute(
        sa.text(
            """
            SELECT DISTINCT u.id, u.organization_id
            FROM staff_users AS u
            JOIN resource_grants AS g
              ON g.organization_id = u.organization_id
             AND g.subject_id = u.id
             AND g.resource_type = 'knowledge'
             AND g.actions @> ARRAY['knowledge.review']::varchar[]
            JOIN knowledge_bases AS kb
              ON kb.id = g.resource_id
             AND kb.organization_id = u.organization_id
            WHERE u.status = 'ACTIVE' AND u.role IN ('ADMIN', 'REVIEWER')
            """
        )
    ).all()
    for user_id, organization_id in rows:
        for capability in ("email", "support"):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO resource_grants
                        (id, organization_id, subject_id, resource_type, resource_id, actions)
                    VALUES
                        (:id, :organization_id, :subject_id, 'application', :resource_id,
                         ARRAY['application.access']::varchar[])
                    ON CONFLICT (organization_id, subject_id, resource_type, resource_id)
                    DO NOTHING
                    """
                ),
                {
                    "id": uuid4(),
                    "organization_id": organization_id,
                    "subject_id": user_id,
                    "resource_id": _application_resource_id(organization_id, capability),
                },
            )

    op.execute("GRANT SELECT, INSERT, DELETE ON TABLE gmail_push_receipts TO platform_app")
    op.execute(
        "GRANT UPDATE (mailbox_address, processed_history_id, pending_history_id, "
        "sync_start_history_id, pending_page_token, page_token_generation, active_sync_job_id, "
        "sync_generation, sync_status, auto_sync_enabled, watch_status, "
        "watch_operation_generation, watch_topic_name, watch_expiration_at, watch_history_id, "
        "last_watch_success_at, watch_last_error_code, last_notification_at, recovery_batch_id, "
        "recovery_baseline_history_id, recovery_window_started_at, recovery_page_token, "
        "recovery_processed_count, recovery_message_limit, recovery_has_more, "
        "recovery_started_at, last_error_code, "
        "last_success_at, updated_at, version) ON email_sync_states TO platform_app"
    )
    op.execute(
        "GRANT UPDATE (active_draft_job_id) ON email_work_items TO platform_app"
    )
    op.execute(
        "GRANT UPDATE (attempt_cycle, cycle_start_attempts, cycle_attempt_limit) "
        "ON job_intents TO platform_app"
    )


def downgrade() -> None:
    op.execute("DELETE FROM resource_grants WHERE resource_type = 'application'")
    op.execute("REVOKE SELECT, INSERT, DELETE ON TABLE gmail_push_receipts FROM platform_app")
    op.drop_index("ix_gmail_push_receipts_expiry", table_name="gmail_push_receipts")
    op.drop_table("gmail_push_receipts")
    for name in ("cycle_attempt_limit", "cycle_start_attempts", "attempt_cycle"):
        op.drop_column("job_intents", name)
    op.drop_constraint(
        "fk_email_work_items_active_draft_job", "email_work_items", type_="foreignkey"
    )
    op.drop_column("email_work_items", "active_draft_job_id")
    op.drop_constraint("fk_email_sync_states_active_job", "email_sync_states", type_="foreignkey")
    op.drop_constraint("uq_email_sync_states_mailbox", "email_sync_states", type_="unique")
    for name in (
        "version", "recovery_started_at", "recovery_has_more", "recovery_message_limit",
        "recovery_processed_count",
        "recovery_page_token", "recovery_window_started_at", "recovery_baseline_history_id",
        "recovery_batch_id", "last_notification_at", "watch_last_error_code",
        "last_watch_success_at", "watch_history_id", "watch_expiration_at", "watch_topic_name",
        "watch_operation_generation", "watch_status", "auto_sync_enabled", "sync_status",
        "sync_generation", "active_sync_job_id", "page_token_generation", "sync_start_history_id",
        "pending_history_id", "mailbox_address",
    ):
        op.drop_column("email_sync_states", name)
    op.alter_column(
        "email_sync_states", "processed_history_id", new_column_name="history_id"
    )
    op.execute("DROP TYPE gmail_sync_status")
    op.execute("DROP TYPE gmail_watch_status")
