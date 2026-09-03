import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.core.database import async_sessionmaker, engine
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.retention.models import ErasureRequest, ErasureScope, ErasureStatus


@pytest.mark.asyncio
async def test_replay_cli_blocks_then_releases_readiness_across_sessions() -> None:
    database_url = os.environ["DATABASE_URL"]
    async with async_sessionmaker() as setup:
        organization = Organization(name=f"Replay CLI {uuid4()}")
        setup.add(organization)
        await setup.flush()
        admin = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"replay-cli-{uuid4()}",
            email="replay-cli@example.test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        setup.add(admin)
        await setup.flush()
        request = ErasureRequest(
            organization_id=organization.id,
            requested_by_id=admin.id,
            subject_key_hash="a" * 64,
            scope=ErasureScope.CUSTOMER,
            status=ErasureStatus.PENDING,
        )
        setup.add(request)
        await setup.commit()
        organization_id = organization.id
        request_id = request.id

    script = Path(__file__).resolve().parents[4] / "scripts" / "replay-erasure-ledger"
    command = [
        str(script),
        "--database-url",
        database_url,
        "--restore-generation",
        "2",
    ]
    try:
        blocked = subprocess.run([*command, "--check"], check=False, timeout=30)
        replayed = subprocess.run(command, check=False, timeout=30)
        ready = subprocess.run([*command, "--check"], check=False, timeout=30)

        assert blocked.returncode == 1
        assert replayed.returncode == 0
        assert ready.returncode == 0
        async with async_sessionmaker() as verify:
            persisted = await verify.get(ErasureRequest, request_id)
            assert persisted is not None
            assert persisted.status is ErasureStatus.APPLIED
            assert persisted.replay_generation == 2
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(ErasureRequest).where(ErasureRequest.id == request_id))
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()
