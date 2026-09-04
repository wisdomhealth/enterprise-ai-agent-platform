"""Harden email privileges, provenance, leases, and evaluation evidence.

Revision ID: 0017_email_ingestion_hardening
Revises: 0016_email_ingestion
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_email_ingestion_hardening"
down_revision: str | None = "0016_email_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "email_state_history_actor_id_fkey", "email_state_history", type_="foreignkey"
    )
    op.add_column(
        "email_state_history",
        sa.Column(
            "actor_type",
            sa.String(length=16),
            server_default=sa.text("'SYSTEM'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_email_state_history_actor_type",
        "email_state_history",
        "actor_type IN ('SYSTEM', 'STAFF')",
    )

    op.add_column(
        "email_evaluation_runs",
        sa.Column(
            "metrics_version",
            sa.String(length=100),
            server_default=sa.text("'email-category-only-v1'"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_evaluation_runs", sa.Column("category_macro_f1", sa.Float(), nullable=True)
    )
    op.add_column(
        "email_evaluation_runs", sa.Column("priority_macro_f1", sa.Float(), nullable=True)
    )
    op.add_column(
        "email_evaluation_runs", sa.Column("reply_required_f1", sa.Float(), nullable=True)
    )
    op.add_column("email_evaluation_runs", sa.Column("exact_match_rate", sa.Float(), nullable=True))
    for column in (
        "category_macro_f1",
        "priority_macro_f1",
        "reply_required_f1",
        "exact_match_rate",
    ):
        op.create_check_constraint(
            f"ck_email_eval_{column}",
            "email_evaluation_runs",
            f"{column} IS NULL OR ({column} >= 0 AND {column} <= 1)",
        )
    op.create_check_constraint(
        "ck_email_eval_complete_v2_metrics",
        "email_evaluation_runs",
        "metrics_version <> 'email-classification-v2' OR "
        "(category_macro_f1 IS NOT NULL AND priority_macro_f1 IS NOT NULL AND "
        "reply_required_f1 IS NOT NULL AND exact_match_rate IS NOT NULL)",
    )

    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE email_work_items, email_state_history, "
        "email_sync_states, email_evaluation_runs FROM platform_app"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE email_work_items TO platform_app")
    op.execute("GRANT SELECT, INSERT ON TABLE email_state_history TO platform_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE email_sync_states TO platform_app")
    op.execute("GRANT SELECT, INSERT ON TABLE email_evaluation_runs TO platform_app")


def downgrade() -> None:
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE email_work_items, email_state_history, "
        "email_sync_states, email_evaluation_runs FROM platform_app"
    )
    op.drop_constraint("ck_email_eval_complete_v2_metrics", "email_evaluation_runs", type_="check")
    for column in reversed(
        (
            "category_macro_f1",
            "priority_macro_f1",
            "reply_required_f1",
            "exact_match_rate",
        )
    ):
        op.drop_constraint(f"ck_email_eval_{column}", "email_evaluation_runs", type_="check")
        op.drop_column("email_evaluation_runs", column)
    op.drop_column("email_evaluation_runs", "metrics_version")
    op.execute("UPDATE email_state_history SET actor_id = NULL WHERE actor_type = 'SYSTEM'")
    op.drop_constraint("ck_email_state_history_actor_type", "email_state_history", type_="check")
    op.drop_column("email_state_history", "actor_type")
    op.create_foreign_key(
        "email_state_history_actor_id_fkey",
        "email_state_history",
        "staff_users",
        ["actor_id"],
        ["id"],
        ondelete="SET NULL",
    )
