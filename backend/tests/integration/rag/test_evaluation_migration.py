import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
PREVIOUS_REVISION = "0011_rag_evaluation_runs"
SNAPSHOT_REVISION = "0012_rag_evaluation_case_snapshots"


def _owner_dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _application_dsn() -> str:
    return _owner_dsn().replace("postgres@", "platform_app@", 1)


def _run_alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_DIR,
        check=False,
        capture_output=True,
        text=True,
    )


async def _seed_0011_case(
    *,
    run_id: UUID,
    case_id: UUID,
    retrieved_chunk_ids: list[UUID],
    retrieved_document_ids: list[UUID],
    retrieved_version_ids: list[UUID],
    citation_chunk_ids: list[UUID],
    citation_version_ids: list[UUID],
) -> None:
    connection = await asyncpg.connect(_owner_dsn())
    try:
        await connection.execute(
            """
            INSERT INTO rag_evaluation_runs
            (id, organization_id, knowledge_base_id, dataset_version, dataset_kind, dataset_digest,
             document_version_set, chunking_version, embedding_model, retrieval_config,
             prompt_version, llm_model, status, metrics, hard_gates, completed_at)
            VALUES ($1, $2, $3, 'legacy-v1', 'regression', $4, 'legacy-docs', 'legacy-chunks',
                    'legacy-embedding', '{}'::jsonb, 'legacy-prompt', 'legacy-model',
                    'COMPLETED', '{}'::jsonb, '{}'::jsonb, now())
            """,
            run_id,
            uuid4(),
            uuid4(),
            "a" * 64,
        )
        await connection.execute(
            """
            INSERT INTO rag_evaluation_cases
            (id, run_id, case_id, question_digest, answer_refused,
             retrieved_chunk_ids, retrieved_document_ids, retrieved_document_version_ids,
             citation_chunk_ids, citation_document_version_ids)
            VALUES ($1, $2, 'legacy-refund-case', $3, true, $4, $5, $6, $7, $8)
            """,
            case_id,
            run_id,
            "b" * 64,
            retrieved_chunk_ids,
            retrieved_document_ids,
            retrieved_version_ids,
            citation_chunk_ids,
            citation_version_ids,
        )
    finally:
        await connection.close()


async def _read_case(dsn: str, case_id: UUID) -> asyncpg.Record | None:
    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchrow(
            """
            SELECT case_id, question_digest, answer_refused, retrieved_chunk_ids,
                   retrieved_document_ids, retrieved_document_version_ids,
                   citation_chunk_ids, citation_document_version_ids, snapshot
            FROM rag_evaluation_cases WHERE id = $1
            """,
            case_id,
        )
    finally:
        await connection.close()


async def _assert_application_role_cannot_mutate_case(case_id: UUID) -> None:
    connection = await asyncpg.connect(_application_dsn())
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                """
                UPDATE rag_evaluation_cases
                SET retrieved_chunk_ids = ARRAY[]::uuid[], snapshot = '{}'::jsonb
                WHERE id = $1
                """,
                case_id,
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("DELETE FROM rag_evaluation_cases WHERE id = $1", case_id)
    finally:
        await connection.close()


async def _delete_run(run_id: UUID) -> None:
    connection = await asyncpg.connect(_owner_dsn())
    try:
        await connection.execute("DELETE FROM rag_evaluation_cases WHERE run_id = $1", run_id)
        await connection.execute("DELETE FROM rag_evaluation_runs WHERE id = $1", run_id)
    finally:
        await connection.close()


def test_0012_backfills_exact_0011_case_values_and_keeps_them_immutable() -> None:
    run_id = uuid4()
    case_id = uuid4()
    retrieved_chunk_ids = [uuid4(), uuid4()]
    retrieved_document_ids = [uuid4(), uuid4()]
    retrieved_version_ids = [uuid4(), uuid4()]
    citation_chunk_ids = [retrieved_chunk_ids[1]]
    citation_version_ids = [retrieved_version_ids[1]]

    assert _run_alembic("upgrade", "head").returncode == 0
    downgrade = _run_alembic("downgrade", PREVIOUS_REVISION)
    assert downgrade.returncode == 0, downgrade.stderr
    try:
        asyncio.run(
            _seed_0011_case(
                run_id=run_id,
                case_id=case_id,
                retrieved_chunk_ids=retrieved_chunk_ids,
                retrieved_document_ids=retrieved_document_ids,
                retrieved_version_ids=retrieved_version_ids,
                citation_chunk_ids=citation_chunk_ids,
                citation_version_ids=citation_version_ids,
            )
        )

        upgrade = _run_alembic("upgrade", SNAPSHOT_REVISION)
        assert upgrade.returncode == 0, upgrade.stderr

        stored = asyncio.run(_read_case(_owner_dsn(), case_id))
        assert stored is not None
        assert stored["case_id"] == "legacy-refund-case"
        assert stored["question_digest"] == "b" * 64
        assert stored["answer_refused"] is True
        assert list(stored["retrieved_chunk_ids"]) == retrieved_chunk_ids
        assert list(stored["retrieved_document_ids"]) == retrieved_document_ids
        assert list(stored["retrieved_document_version_ids"]) == retrieved_version_ids
        assert list(stored["citation_chunk_ids"]) == citation_chunk_ids
        assert list(stored["citation_document_version_ids"]) == citation_version_ids
        assert json.loads(stored["snapshot"]) == {
            "legacy": True,
            "case_id": "legacy-refund-case",
            "question_digest": "b" * 64,
            "answer_refused": True,
            "retrieved_chunk_ids": [str(value) for value in retrieved_chunk_ids],
            "retrieved_document_ids": [str(value) for value in retrieved_document_ids],
            "retrieved_document_version_ids": [str(value) for value in retrieved_version_ids],
            "citation_chunk_ids": [str(value) for value in citation_chunk_ids],
            "citation_document_version_ids": [str(value) for value in citation_version_ids],
        }

        asyncio.run(_assert_application_role_cannot_mutate_case(case_id))
        assert asyncio.run(_read_case(_application_dsn(), case_id)) == stored
    finally:
        restore = _run_alembic("upgrade", "head")
        assert restore.returncode == 0, restore.stderr
        asyncio.run(_delete_run(run_id))
