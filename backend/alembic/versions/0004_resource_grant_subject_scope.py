"""Enforce organization scope for resource-grant subjects."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_grant_subject_scope"
down_revision: str | None = "0003_resource_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INCONSISTENT_GRANTS_MESSAGE = (
    "Cannot upgrade 0004_resource_grant_subject_scope: a resource grant's organization "
    "does not match its subject. Correct or remove every listed grant through an audited "
    "operational process, then retry. Grant IDs: {grant_ids}"
)


def upgrade() -> None:
    resource_grants = sa.table(
        "resource_grants",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
        sa.column("subject_id", sa.Uuid()),
    )
    staff_users = sa.table(
        "staff_users",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
    )
    scoped_subject = sa.and_(
        staff_users.c.id == resource_grants.c.subject_id,
        staff_users.c.organization_id == resource_grants.c.organization_id,
    )
    inconsistent_grant_ids = (
        op.get_bind()
        .execute(
            sa.select(resource_grants.c.id)
            .select_from(resource_grants.outerjoin(staff_users, scoped_subject))
            .where(staff_users.c.id.is_(None))
            .order_by(resource_grants.c.id)
        )
        .scalars()
        .all()
    )
    if inconsistent_grant_ids:
        raise RuntimeError(
            INCONSISTENT_GRANTS_MESSAGE.format(
                grant_ids=", ".join(str(grant_id) for grant_id in inconsistent_grant_ids)
            )
        )

    op.create_unique_constraint(
        "uq_staff_users_organization_id_id",
        "staff_users",
        ["organization_id", "id"],
    )
    op.drop_constraint(
        "resource_grants_subject_id_fkey",
        "resource_grants",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_resource_grants_organization_subject",
        "resource_grants",
        "staff_users",
        ["organization_id", "subject_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_resource_grants_organization_subject",
        "resource_grants",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "resource_grants_subject_id_fkey",
        "resource_grants",
        "staff_users",
        ["subject_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_staff_users_organization_id_id",
        "staff_users",
        type_="unique",
    )
