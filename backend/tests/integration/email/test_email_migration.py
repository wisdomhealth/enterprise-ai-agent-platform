import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

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
    restore = _run_alembic("upgrade", "head")
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
    restore = _run_alembic("upgrade", "head")
    assert restore.returncode == 0, restore.stderr


def _owner_dsn() -> str:
    return os.environ["MIGRATION_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


def _application_dsn() -> str:
    return make_url(_owner_dsn()).set(username="platform_app").render_as_string(
        hide_password=False
    )


def _application_resource_id(organization_id, capability: str):  # type: ignore[no-untyped-def]
    return uuid5(NAMESPACE_URL, f"application-access:{organization_id}:{capability}")


def test_0022_preserves_cursor_and_backfills_only_safe_access_and_draft_state() -> None:
    assert _run_alembic("upgrade", "0021_webhook_subscriptions").returncode == 0
    assert _run_alembic("downgrade", "0021_webhook_subscriptions").returncode == 0

    organization_id = uuid4()
    other_organization_id = uuid4()
    knowledge_base_id = uuid4()
    other_knowledge_base_id = uuid4()
    connector_id = uuid4()
    secret_id = uuid4()
    users = {
        "admin": uuid4(),
        "reviewer": uuid4(),
        "member": uuid4(),
        "disabled": uuid4(),
        "cross_org": uuid4(),
    }
    work_items = {
        "single": uuid4(),
        "ambiguous": uuid4(),
        "completed": uuid4(),
    }
    jobs = {
        "single": uuid4(),
        "ambiguous_one": uuid4(),
        "ambiguous_two": uuid4(),
        "completed": uuid4(),
    }

    async def seed_and_assert() -> None:
        connection = await asyncpg.connect(_owner_dsn())
        try:
            await connection.executemany(
                "INSERT INTO organizations (id, name) VALUES ($1, $2)",
                [
                    (organization_id, "Migration organization"),
                    (other_organization_id, "Other migration organization"),
                ],
            )
            await connection.executemany(
                """
                INSERT INTO knowledge_bases (id, organization_id, public_key)
                VALUES ($1, $2, $3)
                """,
                [
                    (knowledge_base_id, organization_id, f"migration-{uuid4().hex}"),
                    (
                        other_knowledge_base_id,
                        other_organization_id,
                        f"migration-{uuid4().hex}",
                    ),
                ],
            )
            await connection.executemany(
                """
                INSERT INTO staff_users (id, organization_id, email, role, status)
                VALUES ($1, $2, $3, $4::user_role, $5::user_status)
                """,
                [
                    (users["admin"], organization_id, "admin@example.test", "ADMIN", "ACTIVE"),
                    (
                        users["reviewer"],
                        organization_id,
                        "reviewer@example.test",
                        "REVIEWER",
                        "ACTIVE",
                    ),
                    (users["member"], organization_id, "member@example.test", "MEMBER", "ACTIVE"),
                    (
                        users["disabled"],
                        organization_id,
                        "disabled@example.test",
                        "REVIEWER",
                        "DISABLED",
                    ),
                    (
                        users["cross_org"],
                        other_organization_id,
                        "cross@example.test",
                        "REVIEWER",
                        "ACTIVE",
                    ),
                ],
            )
            grants = []
            for label in ("admin", "reviewer", "member", "disabled"):
                grants.append(
                    (
                        uuid4(),
                        organization_id,
                        users[label],
                        knowledge_base_id,
                    )
                )
            grants.append(
                (
                    uuid4(),
                    other_organization_id,
                    users["cross_org"],
                    other_knowledge_base_id,
                )
            )
            await connection.executemany(
                """
                INSERT INTO resource_grants
                    (id, organization_id, subject_id, resource_type, resource_id, actions)
                VALUES ($1, $2, $3, 'knowledge', $4,
                        ARRAY['knowledge.review']::varchar[])
                """,
                grants,
            )
            await connection.execute(
                """
                INSERT INTO connector_secrets
                    (id, organization_id, ciphertext, encrypted_data_key, nonce,
                     algorithm, key_version)
                VALUES ($1, $2, 'x', 'x', 'x', 'AES-256-GCM', 'fixture')
                """,
                secret_id,
                organization_id,
            )
            await connection.execute(
                """
                INSERT INTO connectors (id, organization_id, kind, status, secret_id)
                VALUES ($1, $2, 'GMAIL', 'ACTIVE', $3)
                """,
                connector_id,
                organization_id,
                secret_id,
            )
            await connection.execute(
                """
                INSERT INTO email_sync_states (organization_id, connector_id, history_id)
                VALUES ($1, $2, '9007199254740993')
                """,
                organization_id,
                connector_id,
            )
            for label, state in (
                ("single", "DRAFTING"),
                ("ambiguous", "DRAFT_RETRY_WAIT"),
                ("completed", "AWAITING_REVIEW"),
            ):
                await connection.execute(
                    """
                    INSERT INTO email_work_items
                        (id, organization_id, connector_id, knowledge_base_id,
                         gmail_message_id, gmail_thread_id, sender, recipients,
                         subject, body, received_at, raw_content_ref, state)
                    VALUES ($1, $2, $3, $4, $5, $6, 'sender@example.test',
                            '[]'::jsonb, 'subject', 'body', now(), $7,
                            $8::email_state)
                    """,
                    work_items[label],
                    organization_id,
                    connector_id,
                    knowledge_base_id,
                    f"message-{label}-{uuid4()}",
                    f"thread-{label}",
                    f"gmail://fixture/{label}",
                    state,
                )
            job_rows = [
                (jobs["single"], "PENDING", work_items["single"], 1),
                (jobs["ambiguous_one"], "PENDING", work_items["ambiguous"], 2),
                (jobs["ambiguous_two"], "RUNNING", work_items["ambiguous"], 3),
                (jobs["completed"], "SUCCEEDED", work_items["completed"], 1),
            ]
            await connection.executemany(
                """
                INSERT INTO job_intents
                    (id, kind, idempotency_key, payload, state, attempts)
                VALUES ($1, 'email.draft', $2,
                        jsonb_build_object('work_item_id', $3::text),
                        $4::job_state, $5)
                """,
                [
                    (job_id, f"migration-{job_id}", str(work_item_id), state, attempts)
                    for job_id, state, work_item_id, attempts in job_rows
                ],
            )
        finally:
            await connection.close()

        upgrade = _run_alembic("upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr

        connection = await asyncpg.connect(_owner_dsn())
        try:
            assert await connection.fetchval(
                "SELECT processed_history_id FROM email_sync_states WHERE connector_id = $1",
                connector_id,
            ) == "9007199254740993"
            assert await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_email_sync_states_mailbox'
                )
                """
            )
            assert await connection.fetchval(
                "SELECT active_draft_job_id FROM email_work_items WHERE id = $1",
                work_items["single"],
            ) == jobs["single"]
            assert await connection.fetchrow(
                """
                SELECT attempt_cycle, cycle_start_attempts, cycle_attempt_limit
                FROM job_intents WHERE id = $1
                """,
                jobs["single"],
            ) == (1, 1, 3)
            ambiguous = await connection.fetchrow(
                """
                SELECT state, last_error_code, active_draft_job_id
                FROM email_work_items WHERE id = $1
                """,
                work_items["ambiguous"],
            )
            assert tuple(ambiguous) == (
                "FAILED_TERMINAL",
                "EMAIL_DRAFT_CANONICAL_JOB_AMBIGUOUS",
                None,
            )
            ambiguous_job_rows = await connection.fetch(
                "SELECT state FROM job_intents WHERE id = ANY($1::uuid[]) ORDER BY id",
                [jobs["ambiguous_one"], jobs["ambiguous_two"]],
            )
            assert [row["state"] for row in ambiguous_job_rows] == ["FAILED", "FAILED"]
            assert await connection.fetchval(
                "SELECT state FROM job_intents WHERE id = $1", jobs["completed"]
            ) == "SUCCEEDED"
            assert await connection.fetchval(
                "SELECT active_draft_job_id FROM email_work_items WHERE id = $1",
                work_items["completed"],
            ) is None

            for label in ("admin", "reviewer", "cross_org"):
                subject_organization_id = (
                    other_organization_id if label == "cross_org" else organization_id
                )
                assert await connection.fetchval(
                    """
                    SELECT count(*) FROM resource_grants
                    WHERE subject_id = $1 AND organization_id = $2
                      AND resource_type = 'application'
                      AND resource_id = ANY($3::uuid[])
                      AND actions @> ARRAY['application.access']::varchar[]
                    """,
                    users[label],
                    subject_organization_id,
                    [
                        _application_resource_id(subject_organization_id, "email"),
                        _application_resource_id(subject_organization_id, "support"),
                    ],
                ) == 2
            for label in ("member", "disabled"):
                assert await connection.fetchval(
                    """
                    SELECT count(*) FROM resource_grants
                    WHERE subject_id = $1 AND resource_type = 'application'
                    """,
                    users[label],
                ) == 0
            assert await connection.fetchval(
                """
                SELECT count(*) FROM resource_grants
                WHERE resource_type = 'knowledge'
                  AND organization_id = ANY($1::uuid[])
                """,
                [organization_id, other_organization_id],
            ) == 5
        finally:
            await connection.close()

    asyncio.run(seed_and_assert())


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
            INSERT INTO email_sync_states
                (organization_id, connector_id, processed_history_id)
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
