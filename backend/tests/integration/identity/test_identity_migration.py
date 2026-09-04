import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

BACKEND_DIR = Path(__file__).resolve().parents[3]
DOWNGRADE_BLOCKED_MESSAGE = (
    "Cannot downgrade 0002_identity_sessions while unbound staff invitations exist"
)


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


async def insert_unbound_invitation() -> tuple[UUID, UUID]:
    organization_id = uuid4()
    user_id = uuid4()
    connection = await asyncpg.connect(database_dsn())
    try:
        await connection.execute(
            "INSERT INTO organizations (id, name) VALUES ($1, $2)",
            organization_id,
            "Downgrade Safety",
        )
        await connection.execute(
            """
            INSERT INTO staff_users
                (id, organization_id, oidc_subject, email, role, status, version)
            VALUES
                ($1, $2, NULL, $3, 'MEMBER', 'INVITED', 1)
            """,
            user_id,
            organization_id,
            "unbound-downgrade@example.com",
        )
    finally:
        await connection.close()
    return organization_id, user_id


async def read_invitation_and_revision(user_id: UUID) -> tuple[object, str]:
    connection = await asyncpg.connect(database_dsn())
    try:
        oidc_subject = await connection.fetchval(
            "SELECT oidc_subject FROM staff_users WHERE id = $1",
            user_id,
        )
        revision = await connection.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await connection.close()
    return oidc_subject, revision


async def delete_organization(organization_id: UUID) -> None:
    connection = await asyncpg.connect(database_dsn())
    try:
        await connection.execute("DELETE FROM organizations WHERE id = $1", organization_id)
    finally:
        await connection.close()


def test_downgrade_refuses_unbound_invitation_without_losing_identity_data():
    assert run_alembic("upgrade", "head").returncode == 0
    organization_id, user_id = asyncio.run(insert_unbound_invitation())
    _, starting_revision = asyncio.run(read_invitation_and_revision(user_id))
    try:
        downgrade = run_alembic("downgrade", "0001_platform_foundation")

        assert downgrade.returncode != 0
        assert DOWNGRADE_BLOCKED_MESSAGE in f"{downgrade.stdout}\n{downgrade.stderr}"
        oidc_subject, revision = asyncio.run(read_invitation_and_revision(user_id))
        assert oidc_subject is None
        assert revision == starting_revision
    finally:
        asyncio.run(delete_organization(organization_id))

    safe_downgrade = run_alembic("downgrade", "0001_platform_foundation")
    assert safe_downgrade.returncode == 0, safe_downgrade.stderr
    restore = run_alembic("upgrade", "head")
    assert restore.returncode == 0, restore.stderr
