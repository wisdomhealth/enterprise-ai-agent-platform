"""Fence idempotency executors with durable lease tokens."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_idempotency_lease_fencing"
down_revision: str | None = "0005_audit_outbox_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "idempotency_records",
        sa.Column(
            "lease_token",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("idempotency_records", "lease_token")
