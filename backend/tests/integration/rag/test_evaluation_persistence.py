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
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    return url.replace("postgres@", "platform_app@", 1)


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
