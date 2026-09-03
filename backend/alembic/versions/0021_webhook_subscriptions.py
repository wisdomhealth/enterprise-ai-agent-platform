"""Add encrypted webhook subscriptions and durable delivery intents.

Revision ID: 0021_webhook_subscriptions
Revises: 0020_retention_and_erasure
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021_webhook_subscriptions"
down_revision: str | None = "0020_retention_and_erasure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE webhook_subscription_status AS ENUM ('ACTIVE', 'DISABLED')")
    op.execute(
        "CREATE TYPE webhook_delivery_state AS ENUM "
        "('PENDING', 'DELIVERING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED')"
    )
    op.create_table(
        "webhook_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=False),
        sa.Column("event_types", postgresql.ARRAY(sa.String(length=150)), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="webhook_subscription_status", create_type=False),
            server_default=sa.text("'ACTIVE'::webhook_subscription_status"),
            nullable=False,
        ),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_encrypted_data_key", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("secret_algorithm", sa.String(length=64), nullable=False),
        sa.Column("secret_key_version", sa.String(length=512), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "cardinality(event_types) > 0",
            name="ck_webhook_subscriptions_event_types_nonempty",
        ),
        sa.CheckConstraint("version > 0", name="ck_webhook_subscriptions_version_positive"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_id"],
            ["staff_users.organization_id", "staff_users.id"],
            name="fk_webhook_subscriptions_organization_creator",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_subscriptions_dispatch",
        "webhook_subscriptions",
        ["organization_id", "status", "created_at"],
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(name="webhook_delivery_state", create_type=False),
            server_default=sa.text("'PENDING'::webhook_delivery_state"),
            nullable=False,
        ),
        sa.Column("delivery_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("response_summary", sa.String(length=160), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "delivery_attempt >= 0", name="ck_webhook_deliveries_attempt_nonnegative"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["webhook_subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_events.event_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["job_intents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id", "event_id", name="uq_webhook_deliveries_subscription_event"
        ),
        sa.UniqueConstraint("job_id", name="uq_webhook_deliveries_job"),
    )
    op.create_index(
        "ix_webhook_deliveries_recovery",
        "webhook_deliveries",
        ["state", "updated_at"],
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE webhook_subscriptions, webhook_deliveries TO platform_app"
    )
    op.execute(
        "GRANT UPDATE (status, version, updated_at) ON webhook_subscriptions TO platform_app"
    )
    op.execute(
        "GRANT UPDATE (state, delivery_attempt, last_http_status, response_summary, "
        "last_error_code, delivered_at, updated_at) ON webhook_deliveries TO platform_app"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE UPDATE (state, delivery_attempt, last_http_status, response_summary, "
        "last_error_code, delivered_at, updated_at) ON webhook_deliveries FROM platform_app"
    )
    op.execute(
        "REVOKE UPDATE (status, version, updated_at) ON webhook_subscriptions FROM platform_app"
    )
    op.execute(
        "REVOKE SELECT, INSERT ON TABLE webhook_subscriptions, webhook_deliveries FROM platform_app"
    )
    op.drop_index("ix_webhook_deliveries_recovery", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_subscriptions_dispatch", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
    op.execute("DROP TYPE webhook_delivery_state")
    op.execute("DROP TYPE webhook_subscription_status")
