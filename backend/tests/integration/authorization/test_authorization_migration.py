import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.authorization.models import ResourceGrant
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus

BACKEND_DIR = Path(__file__).resolve().parents[3]
LEGACY_REVISION = "0003_resource_grants"
PREVIOUS_REVISION = "0002_identity_sessions"
INCONSISTENT_GRANT_MESSAGE = "organization does not match its subject"


def database_dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def run_alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_DIR,
        check=False,
        capture_output=True,
        text=True,
    )


async def install_legacy_0003_schema() -> None:
    connection = await asyncpg.connect(database_dsn())
    try:
        await connection.execute(
            """
            CREATE TABLE resource_grants (
                id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                organization_id uuid NOT NULL,
                subject_id uuid NOT NULL,
                resource_type varchar(100) NOT NULL,
                resource_id uuid NOT NULL,
                actions varchar(100)[] NOT NULL,
                CONSTRAINT ck_resource_grants_actions_nonempty
                    CHECK (cardinality(actions) > 0),
                CONSTRAINT resource_grants_organization_id_fkey
                    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
                CONSTRAINT resource_grants_subject_id_fkey
                    FOREIGN KEY (subject_id) REFERENCES staff_users(id) ON DELETE CASCADE,
                CONSTRAINT uq_resource_grants_subject_resource
                    UNIQUE (organization_id, subject_id, resource_type, resource_id)
            );
            CREATE INDEX ix_resource_grants_subject_lookup
                ON resource_grants
                (organization_id, subject_id, resource_type, resource_id);
            UPDATE alembic_version SET version_num = '0003_resource_grants';
            """
        )
    finally:
        await connection.close()


async def reset_legacy_schema_to_0002(organization_ids: tuple[UUID, ...] = ()) -> None:
    connection = await asyncpg.connect(database_dsn())
    try:
        await connection.execute("DROP TABLE IF EXISTS resource_grants CASCADE")
        await connection.execute(
            """
            ALTER TABLE staff_users
            DROP CONSTRAINT IF EXISTS uq_staff_users_organization_id_id
            """
        )
        if organization_ids:
            await connection.execute(
                "DELETE FROM organizations WHERE id = ANY($1::uuid[])",
                organization_ids,
            )
        await connection.execute(
            "UPDATE alembic_version SET version_num = '0002_identity_sessions'"
        )
    finally:
        await connection.close()


async def insert_cross_organization_grant(*, commit: bool) -> tuple[UUID, UUID, UUID]:
    owner_organization_id = uuid4()
    subject_organization_id = uuid4()
    subject_id = uuid4()
    grant_id = uuid4()
    connection = await asyncpg.connect(database_dsn())
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(
            "INSERT INTO organizations (id, name) VALUES ($1, $2), ($3, $4)",
            owner_organization_id,
            "Legacy grant owner",
            subject_organization_id,
            "Legacy grant subject",
        )
        await connection.execute(
            """
            INSERT INTO staff_users
                (id, organization_id, oidc_subject, email, role, status, version)
            VALUES ($1, $2, $3, $4, 'MEMBER', 'ACTIVE', 1)
            """,
            subject_id,
            subject_organization_id,
            f"legacy-subject-{subject_id}",
            f"legacy-subject-{subject_id}@example.com",
        )
        await connection.execute(
            """
            INSERT INTO resource_grants
                (id, organization_id, subject_id, resource_type, resource_id, actions)
            VALUES ($1, $2, $3, 'knowledge', $4, ARRAY['knowledge.read'])
            """,
            grant_id,
            owner_organization_id,
            subject_id,
            uuid4(),
        )
    except BaseException:
        await transaction.rollback()
        raise
    else:
        if commit:
            await transaction.commit()
        else:
            await transaction.rollback()
    finally:
        await connection.close()
    return owner_organization_id, subject_organization_id, grant_id


async def read_revision_and_grant(grant_id: UUID) -> tuple[str, bool]:
    connection = await asyncpg.connect(database_dsn())
    try:
        revision = await connection.fetchval("SELECT version_num FROM alembic_version")
        grant_exists = bool(
            await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM resource_grants WHERE id = $1)",
                grant_id,
            )
        )
    finally:
        await connection.close()
    return revision, grant_exists


def prepare_legacy_0003() -> None:
    downgrade = run_alembic("downgrade", PREVIOUS_REVISION)
    assert downgrade.returncode == 0, downgrade.stderr
    asyncio.run(install_legacy_0003_schema())


def restore_current_head(organization_ids: tuple[UUID, ...] = ()) -> None:
    downgrade = run_alembic("downgrade", PREVIOUS_REVISION)
    assert downgrade.returncode == 0, downgrade.stderr
    asyncio.run(reset_legacy_schema_to_0002(organization_ids))
    restore = run_alembic("upgrade", "head")
    assert restore.returncode == 0, restore.stderr


def test_alembic_metadata_has_no_authorization_schema_drift():
    result = run_alembic("check")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.asyncio
async def test_database_rejects_grant_for_subject_from_another_organization(db_session):
    owner_organization = Organization(name="Grant owner organization")
    subject_organization = Organization(name="Grant subject organization")
    db_session.add_all([owner_organization, subject_organization])
    await db_session.flush()
    foreign_subject = StaffUser(
        organization_id=subject_organization.id,
        oidc_subject=f"foreign-grant-subject-{uuid4()}",
        email=f"foreign-grant-{uuid4()}@example.com",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(foreign_subject)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ResourceGrant(
                    organization_id=owner_organization.id,
                    subject_id=foreign_subject.id,
                    resource_type="knowledge",
                    resource_id=uuid4(),
                    actions=["knowledge.read"],
                )
            )
            await db_session.flush()


def test_legacy_0003_upgrade_adds_cross_organization_grant_constraint():
    prepare_legacy_0003()
    try:
        upgrade = run_alembic("upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            asyncio.run(insert_cross_organization_grant(commit=False))
    finally:
        restore_current_head()


def test_legacy_0003_upgrade_refuses_inconsistent_grants_without_data_loss():
    prepare_legacy_0003()
    organization_ids: tuple[UUID, ...] = ()
    try:
        owner_id, subject_organization_id, grant_id = asyncio.run(
            insert_cross_organization_grant(commit=True)
        )
        organization_ids = (owner_id, subject_organization_id)

        upgrade = run_alembic("upgrade", "head")

        assert upgrade.returncode != 0
        assert INCONSISTENT_GRANT_MESSAGE in f"{upgrade.stdout}\n{upgrade.stderr}"
        revision, grant_exists = asyncio.run(read_revision_and_grant(grant_id))
        assert revision == LEGACY_REVISION
        assert grant_exists is True
    finally:
        restore_current_head(organization_ids)
