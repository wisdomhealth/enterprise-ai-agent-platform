from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[4]


def test_recovery_plan_uses_postgres_job_state_after_redis_loss(
    recovery_cli, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    evidence = tmp_path / "recovery.json"
    result = recovery_cli.run(
        "verify-recovery",
        "--compose-file",
        "compose.test.yaml",
        "--evidence",
        str(evidence),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    payload = result.json()
    steps = payload["steps"]
    assert isinstance(steps, list)
    names = [step["name"] for step in steps]
    assert names.index("commit_pending_job") < names.index("flush_redis")
    assert names.index("flush_redis") < names.index("restart_workers")
    assert names.index("restart_workers") < names.index("verify_job_from_postgres")
    assert payload["recovery_authority"] == "postgresql"


def test_test_compose_declares_isolated_backup_store_and_recovery_volumes() -> None:
    compose = (ROOT / "compose.test.yaml").read_text(encoding="utf-8")

    assert "backup-store:" in compose
    assert "backup-store-cert-init:" in compose
    assert "https://task25-local:task25-local-only@backup-store:9000" in compose
    assert '"-fk", "https://127.0.0.1:9000/minio/health/live"' in compose
    assert "task25" in compose
    assert "pgbackrest" in compose
    assert "user: postgres" in compose
    assert "postgres-recovery-data" in compose
    assert "backup-store-certs" in compose
    assert ("pg_isready -h 127.0.0.1 -U postgres -d platform_test && pgbackrest version") in compose


@pytest.mark.asyncio
async def test_live_redis_loss_phase() -> None:
    if os.getenv("TASK25_RECOVERY_PHASE") != "verify-redis-loss":
        pytest.skip("only the guarded Task 25 recovery drill selects this live phase")
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for _ in range(90):
            async with sessions() as session:
                state = await session.scalar(
                    text(
                        "SELECT state::text FROM job_intents "
                        "WHERE id = '25000000-0000-0000-0000-000000000008'"
                    )
                )
            if state == "SUCCEEDED":
                return
            await asyncio.sleep(1)
        raise AssertionError(f"durable pending job did not recover, final state={state!r}")
    finally:
        await engine.dispose()
