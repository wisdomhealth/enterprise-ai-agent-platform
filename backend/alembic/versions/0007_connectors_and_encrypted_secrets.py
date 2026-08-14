"""Add envelope-encrypted Google connector records."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_connectors_and_encrypted_secrets"
down_revision: str | None = "0006_idempotency_lease_fencing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

connector_kind = postgresql.ENUM("DRIVE", "GMAIL", name="connector_kind")
connector_status = postgresql.ENUM("ACTIVE", "REAUTH_REQUIRED", "ERROR", name="connector_status")


def upgrade() -> None:
    bind = op.get_bind()
    # Alembic writes this revision after ``upgrade`` returns. The initial
    # platform migration sized version_num at 32 characters, while this
    # append-only revision identifier is longer; widen before Alembic records
    # the new head without changing published migrations.
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    connector_kind.create(bind, checkfirst=True)
    connector_status.create(bind, checkfirst=True)
    op.create_table(
        "connector_secrets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_data_key", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("algorithm", sa.String(length=64), nullable=False),
        sa.Column("key_version", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "connectors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "kind", postgresql.ENUM(name="connector_kind", create_type=False), nullable=False
        ),
        sa.Column(
            "status", postgresql.ENUM(name="connector_status", create_type=False), nullable=False
        ),
        sa.Column("secret_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["secret_id"], ["connector_secrets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "kind", name="uq_connectors_organization_kind"),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE connector_secrets, "
        "connectors TO platform_app"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE connector_secrets, "
        "connectors FROM platform_app"
    )
    op.drop_table("connectors")
    op.drop_table("connector_secrets")
    bind = op.get_bind()
    connector_status.drop(bind, checkfirst=True)
    connector_kind.drop(bind, checkfirst=True)
