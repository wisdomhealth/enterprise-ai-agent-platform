"""Permit a new durable handoff lifecycle after explicit Resume AI."""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_support_handoff_lifecycles"
down_revision: str | None = "0014_support_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_support_handoffs_session", "support_handoffs", type_="unique")
    op.create_index("ix_support_handoffs_session", "support_handoffs", ["session_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_support_handoffs_session", table_name="support_handoffs")
    op.create_unique_constraint("uq_support_handoffs_session", "support_handoffs", ["session_id"])
