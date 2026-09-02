import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[3]


def _run_alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_DIR,
        check=False,
        capture_output=True,
        text=True,
    )


def test_0017_round_trip_preserves_the_published_0016_boundary() -> None:
    upgrade = _run_alembic("upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    downgrade = _run_alembic("downgrade", "0016_email_ingestion")
    assert downgrade.returncode == 0, downgrade.stderr
    restore = _run_alembic("upgrade", "0017_email_ingestion_hardening")
    assert restore.returncode == 0, restore.stderr


def test_0018_round_trip_creates_current_draft_index() -> None:
    upgrade = _run_alembic("upgrade", "0018_email_review")
    assert upgrade.returncode == 0, upgrade.stderr

    import asyncpg

    async def index_exists() -> bool:
        connection = await asyncpg.connect(_owner_dsn())
        try:
            return bool(
                await connection.fetchval(
                    "SELECT to_regclass('public.ix_email_work_items_current_draft') IS NOT NULL"
                )
            )
        finally:
            await connection.close()

    import asyncio

    assert asyncio.run(index_exists())
    downgrade = _run_alembic("downgrade", "0017_email_ingestion_hardening")
    assert downgrade.returncode == 0, downgrade.stderr
    restore = _run_alembic("upgrade", "0018_email_review")
    assert restore.returncode == 0, restore.stderr


def _owner_dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _application_dsn() -> str:
    return make_url(_owner_dsn()).set(username="platform_app").render_as_string(
        hide_password=False
    )


@pytest.mark.asyncio
async def test_application_role_can_use_email_tables_but_evaluation_runs_are_append_only() -> None:
    owner = await asyncpg.connect(_owner_dsn())
    app = await asyncpg.connect(_application_dsn())
    organization_id = uuid4()
    knowledge_base_id = uuid4()
    secret_id = uuid4()
    connector_id = uuid4()
    work_item_id = uuid4()
    job_id = uuid4()
    history_id = uuid4()
    run_id = uuid4()
    try:
        assert await app.fetchval("SELECT current_user") == "platform_app"
        await owner.execute(
            "INSERT INTO organizations (id, name) VALUES ($1, 'Email privilege owner')",
            organization_id,
        )
        await owner.execute(
            "INSERT INTO knowledge_bases (id, organization_id, public_key) VALUES ($1, $2, $3)",
            knowledge_base_id,
            organization_id,
            f"privilege-{uuid4().hex}",
        )
        await owner.execute(
            """
            INSERT INTO connector_secrets
                (id, organization_id, ciphertext, encrypted_data_key, nonce, algorithm, key_version)
            VALUES ($1, $2, 'x', 'x', 'x', 'AES-256-GCM', 'fixture')
            """,
            secret_id,
            organization_id,
        )
        await owner.execute(
            """
            INSERT INTO connectors (id, organization_id, kind, status, secret_id)
            VALUES ($1, $2, 'GMAIL', 'ACTIVE', $3)
            """,
            connector_id,
            organization_id,
            secret_id,
        )
        await app.execute(
            """
            INSERT INTO email_work_items
                (id, organization_id, connector_id, knowledge_base_id, gmail_message_id,
                 gmail_thread_id, sender, recipients, subject, body, received_at, raw_content_ref)
            VALUES ($1, $2, $3, $4, 'message-1', 'thread-1', 'sender@example.test',
                    '[]'::jsonb, 'subject', 'body', now(), 'gmail://fixture/message-1')
            """,
            work_item_id,
            organization_id,
            connector_id,
            knowledge_base_id,
        )
        await app.execute(
            "UPDATE email_work_items SET last_error_code = 'SAFE_CODE' WHERE id = $1",
            work_item_id,
        )
        await owner.execute(
            """
            INSERT INTO job_intents (id, kind, idempotency_key, payload)
            VALUES ($1, 'email.gmail_history', $2, '{}'::jsonb)
            """,
            job_id,
            f"privilege-{uuid4()}",
        )
        await app.execute(
            """
            INSERT INTO email_state_history
                (id, work_item_id, organization_id, from_state, to_state, action,
                 actor_id, actor_type, job_id, resource_version)
            VALUES ($1, $2, $3, 'INGESTED', 'DRAFTING', 'START_DRAFT',
                    $4, 'SYSTEM', $5, 2)
            """,
            history_id,
            work_item_id,
            organization_id,
            uuid4(),
            job_id,
        )
        assert (
            await app.fetchval(
                "SELECT actor_type FROM email_state_history WHERE id = $1", history_id
            )
            == "SYSTEM"
        )
        await app.execute(
            """
            INSERT INTO email_sync_states (organization_id, connector_id, history_id)
            VALUES ($1, $2, 'cursor-1')
            """,
            organization_id,
            connector_id,
        )
        await app.execute(
            """
            INSERT INTO email_evaluation_runs
                (id, dataset_version, dataset_kind, dataset_digest, model, prompt_version,
                 metrics_version, macro_f1, category_macro_f1, priority_macro_f1,
                 reply_required_f1, exact_match_rate, structured_output_success, latency_ms,
                 input_tokens, output_tokens, estimated_cost)
            VALUES ($1, 'v2', 'regression', $2, 'model', 'prompt',
                    'email-classification-v2', 1, 1, 1, 1, 1, 1, 1, 1, 1, 0)
            """,
            run_id,
            "a" * 64,
        )
        assert (
            await app.fetchval(
                "SELECT metrics_version FROM email_evaluation_runs WHERE id = $1", run_id
            )
            == "email-classification-v2"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("UPDATE email_evaluation_runs SET macro_f1 = 0 WHERE id = $1", run_id)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("DELETE FROM email_evaluation_runs WHERE id = $1", run_id)
    finally:
        await app.close()
        await owner.execute("DELETE FROM email_evaluation_runs WHERE id = $1", run_id)
        await owner.execute("DELETE FROM organizations WHERE id = $1", organization_id)
        await owner.close()
