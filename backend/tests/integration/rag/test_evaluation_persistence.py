import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.modules.rag.evaluation import (
    EvaluationCaseProvenance,
    EvaluationMetrics,
    EvaluationRun,
    EvaluationRunRepository,
    HardGateStatus,
)
from app.modules.rag.evaluation_models import RAGEvaluationCase, RAGEvaluationRun


def _application_dsn() -> str:
    return _owner_dsn().replace("postgres@", "platform_app@", 1)


def _owner_dsn() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _delete_evaluation_run(run_id) -> None:  # type: ignore[no-untyped-def]
    connection = await asyncpg.connect(_owner_dsn())
    try:
        await connection.execute("DELETE FROM rag_evaluation_cases WHERE run_id = $1", run_id)
        await connection.execute("DELETE FROM rag_evaluation_runs WHERE id = $1", run_id)
    finally:
        await connection.close()


def _run(run_id):  # type: ignore[no-untyped-def]
    return EvaluationRun(
        id=run_id,
        organization_id=uuid4(),
        knowledge_base_id=uuid4(),
        dataset_version="rag-regression-v1",
        dataset_kind="regression",
        dataset_digest="a" * 64,
        document_version_set="documents-v1",
        chunking_version="chunking-v1",
        embedding_model="embedding-v1",
        retrieval_config={"limit": "10"},
        prompt_version="grounded-answer-v1",
        llm_model="fake-v1",
        status="COMPLETED",
        completed_at=datetime.now(UTC),
        metrics=EvaluationMetrics(
            recall_at_10=1,
            citation_mapping_rate=1,
            citation_support_rate=1,
            abstention_rate=1,
            answer_groundedness=1,
            claim_groundedness=1,
            retrieval_latency_ms=1,
            model_latency_ms=2,
            end_to_end_latency_ms=3,
            input_tokens=4,
            output_tokens=5,
            estimated_cost=0.01,
        ),
        hard_gates=HardGateStatus(
            authorized_candidates_only=True,
            no_forbidden_documents=True,
            citations_map_to_retrieval=True,
        ),
    )


@pytest.mark.asyncio
async def test_evaluation_runs_append_immutable_case_evidence_without_overwriting_history(
    db_session,
) -> None:
    run = _run(uuid4())
    case = EvaluationCaseProvenance(
        case_id="refund-1",
        question_digest="b" * 64,
        answer_refused=False,
        retrieved_chunk_ids=[uuid4()],
        retrieved_document_ids=[uuid4()],
        retrieved_document_version_ids=[uuid4()],
        citation_chunk_ids=[uuid4()],
        citation_document_version_ids=[uuid4()],
        snapshot={"input": {"expected_claims": ["Refunds take five business days."]}},
    )
    repository = EvaluationRunRepository(db_session)

    await repository.append(run, [case])
    await db_session.flush()
    stored = await db_session.scalar(select(RAGEvaluationRun).where(RAGEvaluationRun.id == run.id))
    stored_case = await db_session.scalar(
        select(RAGEvaluationCase).where(RAGEvaluationCase.run_id == run.id)
    )

    assert stored is not None
    assert stored.dataset_digest == "a" * 64
    assert stored.status == "COMPLETED"
    assert stored.completed_at == run.completed_at
    assert stored_case is not None
    assert stored_case.retrieved_chunk_ids == case.retrieved_chunk_ids
    assert stored_case.citation_document_version_ids == case.citation_document_version_ids
    assert stored_case.snapshot == case.snapshot

    db_session.expunge(stored)
    with pytest.raises(IntegrityError):
        await repository.append(run, [case])
        await db_session.flush()


@pytest.mark.asyncio
async def test_application_role_can_append_but_cannot_update_or_delete_evaluation_history() -> None:
    connection = await asyncpg.connect(_application_dsn())
    run_id = uuid4()
    try:
        await connection.execute(
            """
            INSERT INTO rag_evaluation_runs
            (id, organization_id, knowledge_base_id, dataset_version, dataset_kind, dataset_digest,
             document_version_set, chunking_version, embedding_model, retrieval_config,
             prompt_version,
             llm_model, status, metrics, hard_gates, completed_at)
            VALUES ($1, $2, $3, 'v1', 'regression', $4, 'docs', 'chunks', 'embedding',
                    '{}'::jsonb, 'prompt', 'model', 'COMPLETED', '{}'::jsonb, '{}'::jsonb, now())
            """,
            run_id,
            uuid4(),
            uuid4(),
            "c" * 64,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "UPDATE rag_evaluation_runs SET status = 'FAILED' WHERE id = $1", run_id
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("DELETE FROM rag_evaluation_runs WHERE id = $1", run_id)
    finally:
        await connection.close()
        await _delete_evaluation_run(run_id)


@pytest.mark.asyncio
async def test_application_role_cannot_update_or_delete_case_evidence_from_another_session() -> (
    None
):
    run_id = uuid4()
    case_id = uuid4()
    retrieved_chunk_id = uuid4()
    cited_chunk_id = uuid4()
    retrieved_document_id = uuid4()
    retrieved_version_id = uuid4()
    cited_version_id = uuid4()
    snapshot = {
        "input": {"case_id": "legacy-case"},
        "result": {"text": "Refunds take five business days."},
    }
    try:
        writer = await asyncpg.connect(_application_dsn())
        try:
            await writer.execute(
                """
                INSERT INTO rag_evaluation_runs
                (id, organization_id, knowledge_base_id, dataset_version, dataset_kind,
                 dataset_digest, document_version_set, chunking_version, embedding_model,
                 retrieval_config, prompt_version, llm_model, status, metrics, hard_gates,
                 completed_at)
                VALUES ($1, $2, $3, 'v1', 'regression', $4, 'docs', 'chunks', 'embedding',
                        '{}'::jsonb, 'prompt', 'model', 'COMPLETED', '{}'::jsonb,
                        '{}'::jsonb, now())
                """,
                run_id,
                uuid4(),
                uuid4(),
                "d" * 64,
            )
            await writer.execute(
                """
                INSERT INTO rag_evaluation_cases
                (id, run_id, case_id, question_digest, answer_refused,
                 retrieved_chunk_ids, retrieved_document_ids, retrieved_document_version_ids,
                 citation_chunk_ids, citation_document_version_ids, snapshot)
                VALUES ($1, $2, 'legacy-case', $3, false, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                case_id,
                run_id,
                "e" * 64,
                [retrieved_chunk_id],
                [retrieved_document_id],
                [retrieved_version_id],
                [cited_chunk_id],
                [cited_version_id],
                json.dumps(snapshot),
            )
        finally:
            await writer.close()

        mutator = await asyncpg.connect(_application_dsn())
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await mutator.execute(
                    "UPDATE rag_evaluation_cases SET snapshot = '{}'::jsonb WHERE id = $1",
                    case_id,
                )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await mutator.execute("DELETE FROM rag_evaluation_cases WHERE id = $1", case_id)
            stored = await mutator.fetchrow(
                """
                SELECT retrieved_chunk_ids, retrieved_document_ids,
                       retrieved_document_version_ids, citation_chunk_ids,
                       citation_document_version_ids, snapshot
                FROM rag_evaluation_cases WHERE id = $1
                """,
                case_id,
            )
            assert stored is not None
            assert list(stored["retrieved_chunk_ids"]) == [retrieved_chunk_id]
            assert list(stored["retrieved_document_ids"]) == [retrieved_document_id]
            assert list(stored["retrieved_document_version_ids"]) == [retrieved_version_id]
            assert list(stored["citation_chunk_ids"]) == [cited_chunk_id]
            assert list(stored["citation_document_version_ids"]) == [cited_version_id]
            assert json.loads(stored["snapshot"]) == snapshot
        finally:
            await mutator.close()
    finally:
        await _delete_evaluation_run(run_id)
