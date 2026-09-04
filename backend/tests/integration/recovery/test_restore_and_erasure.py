from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.modules.operations.health import ConfiguredHealthReporter, DependencyStatus
from app.modules.retention.service import ErasureService

ROOT = Path(__file__).resolve().parents[4]

_ORGANIZATION_ID = UUID("25000000-0000-0000-0000-000000000001")
_STAFF_ID = UUID("25000000-0000-0000-0000-000000000002")
_KNOWLEDGE_BASE_ID = UUID("25000000-0000-0000-0000-000000000003")
_CHAT_SESSION_ID = UUID("25000000-0000-0000-0000-000000000004")
_CHAT_MESSAGE_ID = UUID("25000000-0000-0000-0000-000000000005")
_ERASURE_ID = UUID("25000000-0000-0000-0000-000000000006")
_ERASURE_TARGET_ID = UUID("25000000-0000-0000-0000-000000000007")
_JOB_ID = UUID("25000000-0000-0000-0000-000000000008")


def test_postgres_archive_and_repository_configuration_is_safe() -> None:
    postgres = (ROOT / "infra/postgres/postgresql.conf").read_text(encoding="utf-8")
    pgbackrest = (ROOT / "infra/pgbackrest/pgbackrest.conf").read_text(encoding="utf-8")
    init = (ROOT / "infra/postgres/entrypoint-initdb.d/10-pgvector.sql").read_text(encoding="utf-8")

    assert "archive_mode = on" in postgres
    assert "pgbackrest" in postgres and "archive-push" in postgres
    assert ".pgbackrest-archive-ready" in postgres
    assert "|| true" not in postgres
    assert "archive_timeout = 900" in postgres
    assert "repo1-type=s3" in pgbackrest
    assert "repo1-cipher-type=aes-256-cbc" in pgbackrest
    assert "repo1-retention-full-type=time" in pgbackrest
    assert "CREATE EXTENSION IF NOT EXISTS vector" in init
    assert "repo1-s3-key=" not in pgbackrest
    assert "repo1-s3-key-secret=" not in pgbackrest
    assert "repo1-cipher-pass=" not in pgbackrest


def test_restore_requires_exact_confirmation_and_an_empty_target(
    recovery_cli, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "restore-target"
    target.mkdir()
    (target / "existing-data").write_text("do not overwrite", encoding="utf-8")

    result = recovery_cli.run(
        "restore-postgres",
        "--target-volume",
        str(target),
        "--target-timestamp",
        "2026-08-09T12:00:00Z",
        "--restore-generation",
        "7",
        "--confirm-empty-target",
        str(target),
        "--dry-run",
    )

    assert result.returncode != 0
    assert "empty" in result.stderr.casefold()
    assert (target / "existing-data").read_text(encoding="utf-8") == "do not overwrite"


def test_restore_plan_keeps_readiness_blocked_until_migration_and_erasure_replay(
    recovery_cli, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "restore-target"
    target.mkdir()

    result = recovery_cli.run(
        "restore-postgres",
        "--target-volume",
        str(target),
        "--target-timestamp",
        "2026-08-09T12:00:00Z",
        "--restore-generation",
        "7",
        "--confirm-empty-target",
        str(target),
        "--database-url",
        "postgresql+asyncpg://platform_app@postgres/platform",
        "--migration-database-url",
        "postgresql+asyncpg://postgres@postgres/platform",
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    payload = result.json()
    assert payload["status"] == "planned"
    steps = payload["steps"]
    assert isinstance(steps, list)
    names = [step["name"] for step in steps]
    assert names == [
        "restore",
        "start_database",
        "migrate",
        "replay_erasure",
        "verify_erasure",
        "rebuild_retrieval",
        "restart_workers",
        "verify_readiness",
    ]
    assert steps[0]["readiness"] == "blocked"
    assert all(step["readiness"] == "blocked" for step in steps[:-1])
    assert steps[-1]["readiness"] == "eligible"
    assert payload["restore_generation"] == 7
    assert payload["target_timestamp"] == "2026-08-09 12:00:00.000000+0000"
    assert "postgresql+asyncpg" not in result.stdout


def test_restore_reindex_uses_the_migration_database_identity() -> None:
    restore = (ROOT / "scripts/restore-postgres").read_text(encoding="utf-8")

    assert "_database_identity(args.migration_database_url)" in restore
    assert "GRANT SELECT ON TABLE alembic_version" in restore
    assert 'os.getenv("POSTGRES_USER", "platform")' not in restore
    assert 'os.getenv("POSTGRES_DB", "platform")' not in restore
    assert '"--wait-timeout"' in restore


def test_recovery_verification_keeps_post_restore_work_on_restored_data() -> None:
    verification = (ROOT / "scripts/verify-recovery").read_text(encoding="utf-8")

    restore_call = verification.index('str(ROOT / "scripts/restore-postgres")')
    source_switch = verification.index('env["POSTGRES_DATA_SOURCE"] = str(target)')
    verify_phase = verification.index("TASK25_RECOVERY_PHASE=verify-restored")
    assert restore_call < source_switch < verify_phase


def test_backup_plan_uses_stanza_check_and_does_not_fabricate_evidence(
    recovery_cli, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    evidence = tmp_path / "backup.json"
    result = recovery_cli.run(
        "backup-postgres",
        "--type",
        "diff",
        "--evidence",
        str(evidence),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    payload = result.json()
    assert payload["status"] == "planned"
    assert payload["backup_type"] == "diff"
    assert payload["commands"] == [
        "stanza-create",
        "enable-archive",
        "check",
        "backup",
        "info",
    ]
    assert payload["repository_encryption"] == "required"
    assert not evidence.exists()


def test_backup_records_the_real_pgbackrest_label_without_credentials(
    recovery_cli, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    fake = tmp_path / "pgbackrest"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "with pathlib.Path(os.environ['TASK25_COMMAND_LOG']).open('a') as log:\n"
        "    log.write(sys.argv[-1] + chr(10))\n"
        "if sys.argv[-1] == 'info':\n"
        "    print(json.dumps([{'backup': [{'label': '20260809-120000F'}]}]))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    evidence = tmp_path / "backup.json"
    log = tmp_path / "commands.log"
    result = recovery_cli.run(
        "backup-postgres",
        "--type",
        "full",
        "--archive-ready-marker",
        str(tmp_path / "archive-ready"),
        "--evidence",
        str(evidence),
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PGBACKREST_REPO1_CIPHER_PASS": "not-recorded",
            "TASK25_COMMAND_LOG": str(log),
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["backup_label"] == "20260809-120000F"
    assert payload["repository_encrypted"] is True
    assert "not-recorded" not in evidence.read_text(encoding="utf-8")
    assert log.read_text(encoding="utf-8").splitlines() == [
        "stanza-create",
        "check",
        "backup",
        "info",
    ]
    assert (tmp_path / "archive-ready").is_file()


def test_recovery_evidence_reports_measurements_without_claiming_an_sla(
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
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["status"] == "planned"
    assert payload["targets"] == {"rpo_seconds": 900, "rto_seconds": 14400}
    assert payload["sla_claimed"] is False
    assert payload["checks"] == [
        "backup",
        "restore_point",
        "migration",
        "erasure_replay",
        "redis_loss_recovery",
        "readiness",
    ]


@pytest.mark.asyncio
async def test_live_recovery_phase() -> None:
    """Provider-free phase entry point used only by the guarded disposable drill."""
    phase = os.getenv("TASK25_RECOVERY_PHASE")
    if phase is None:
        pytest.skip("only the guarded Task 25 recovery drill selects a live phase")
    database_url = os.environ["DATABASE_URL"]
    generation = int(os.getenv("RESTORE_GENERATION", "1"))
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if phase == "seed":
            async with sessions() as session:
                for statement in _seed_statements():
                    await session.execute(statement)
                await session.commit()
            return
        if phase == "apply-erasure":
            async with sessions() as session:
                await ErasureService(session, hash_key=b"task25-test-only").apply(_ERASURE_ID)
                await session.commit()
            return
        if phase == "verify-restored":
            async with sessions() as before:
                assert (
                    await before.scalar(
                        text("SELECT body FROM chat_messages WHERE id = :id"),
                        {"id": _CHAT_MESSAGE_ID},
                    )
                    == ""
                )
            key_path = Path(os.environ["TASK25_TEST_KEY_PATH"])
            reporter = ConfiguredHealthReporter(
                Settings.model_validate(
                    {
                        "DATABASE_URL": database_url,
                        "APP_ENV": "test",
                        "CONNECTOR_FILE_KEY_PATH": key_path,
                        "RESTORE_GENERATION": generation,
                    }
                ),
                sessions,
                redis_client=None,
            )
            recovered = await reporter()
            assert recovered.dependencies["erasure_replay"].status is DependencyStatus.UP
            async with sessions() as replay:
                await ErasureService(
                    replay, hash_key=b"task25-test-only"
                ).replay_pending_and_applied(restore_generation=generation)
                await replay.commit()
            async with sessions() as fresh:
                assert (
                    await fresh.scalar(
                        text("SELECT body FROM chat_messages WHERE id = :id"),
                        {"id": _CHAT_MESSAGE_ID},
                    )
                    == ""
                )
            idempotent = await reporter()
            assert idempotent.dependencies["erasure_replay"].status is DependencyStatus.UP
            return
        raise AssertionError(f"unsupported recovery phase: {phase}")
    finally:
        await engine.dispose()


def _seed_statements() -> tuple[object, ...]:
    return (
        text("INSERT INTO organizations (id, name) VALUES (:id, 'Task 25 recovery')").bindparams(
            id=_ORGANIZATION_ID
        ),
        text(
            "INSERT INTO staff_users "
            "(id, organization_id, oidc_subject, email, role, status) VALUES "
            "(:id, :organization_id, 'task25-recovery', 'task25@example.test', "
            "'ADMIN', 'ACTIVE')"
        ).bindparams(id=_STAFF_ID, organization_id=_ORGANIZATION_ID),
        text(
            "INSERT INTO knowledge_bases (id, organization_id, public_key) "
            "VALUES (:id, :organization_id, 'task25-recovery')"
        ).bindparams(id=_KNOWLEDGE_BASE_ID, organization_id=_ORGANIZATION_ID),
        text(
            "UPDATE retention_policies SET chat_days = 90, email_days = 90, "
            "audit_days = 365, version = 1 WHERE organization_id = :organization_id"
        ).bindparams(organization_id=_ORGANIZATION_ID),
        text(
            "INSERT INTO chat_sessions "
            "(id, organization_id, knowledge_base_id, state, version) "
            "VALUES (:id, :organization_id, :knowledge_base_id, 'AI_ACTIVE', 1)"
        ).bindparams(
            id=_CHAT_SESSION_ID,
            organization_id=_ORGANIZATION_ID,
            knowledge_base_id=_KNOWLEDGE_BASE_ID,
        ),
        text(
            "INSERT INTO chat_messages (id, session_id, sequence, actor, body, status) "
            "VALUES (:id, :session_id, 1, 'CUSTOMER', 'restored private content', 'PERSISTED')"
        ).bindparams(id=_CHAT_MESSAGE_ID, session_id=_CHAT_SESSION_ID),
        text(
            "INSERT INTO erasure_requests "
            "(id, organization_id, requested_by_id, subject_key_hash, scope, status, "
            "replay_generation, verification_counts) VALUES "
            "(:id, :organization_id, :staff_id, :digest, 'CUSTOMER', 'PENDING', 0, '{}'::jsonb)"
        ).bindparams(
            id=_ERASURE_ID,
            organization_id=_ORGANIZATION_ID,
            staff_id=_STAFF_ID,
            digest="a" * 64,
        ),
        text(
            "INSERT INTO erasure_targets (id, request_id, target_type, target_id) "
            "VALUES (:id, :request_id, 'CHAT_SESSION', :target_id)"
        ).bindparams(
            id=_ERASURE_TARGET_ID,
            request_id=_ERASURE_ID,
            target_id=_CHAT_SESSION_ID,
        ),
        text(
            "INSERT INTO job_intents "
            "(id, kind, idempotency_key, payload, state, attempts, version) VALUES "
            "(:id, 'retention.apply_due', 'task25-redis-loss', "
            "CAST(:payload AS jsonb), 'PENDING', 0, 1)"
        ).bindparams(
            id=_JOB_ID,
            payload=json.dumps({"organization_id": str(_ORGANIZATION_ID)}),
        ),
    )
