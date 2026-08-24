from uuid import UUID, uuid4

import pytest

from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.rag.answer_service import AnswerExecution
from app.modules.rag.evaluation import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationDatasetKind,
    EvaluationProvenance,
    RAGEvaluationRunner,
)
from app.modules.rag.types import (
    AnswerAudience,
    ClaimSupport,
    RetrievedChunk,
    SourceCitation,
    ValidatedAnswer,
)


class FakeRetriever:
    def __init__(self, chunk: RetrievedChunk) -> None:
        self.chunk = chunk

    async def retrieve(self, *args: object, **kwargs: object) -> list[RetrievedChunk]:
        return [self.chunk]


class RecordingAnswerService:
    def __init__(self, citation: SourceCitation) -> None:
        self.citation = citation
        self.questions: list[str] = []

    async def answer(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        question: str,
        audience: AnswerAudience,
    ) -> ValidatedAnswer:
        self.questions.append(question)
        return ValidatedAnswer(
            text="Refunds take five business days.",
            claims=[
                ClaimSupport(
                    text="Refunds take five business days.",
                    citation_ids=[self.citation.chunk_id],
                )
            ],
            citations=[self.citation],
            segments=["Refunds take five business days."],
            refused=False,
            model="fake-evaluation-model",
            prompt_version="grounded-answer-v1",
            latency_ms=7,
            input_tokens=5,
            output_tokens=6,
            estimated_cost=0.000105,
        )


class SingleRetrievalAnswerService(RecordingAnswerService):
    def __init__(self, chunk: RetrievedChunk, citation: SourceCitation) -> None:
        super().__init__(citation)
        self.chunk = chunk
        self.evidence_calls = 0

    async def answer_with_evidence(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        question: str,
        audience: AnswerAudience,
    ) -> AnswerExecution:
        self.evidence_calls += 1
        answer = await self.answer(principal, knowledge_base_id, question, audience)
        return AnswerExecution(
            answer=answer,
            retrieved_chunks=[self.chunk],
            retrieval_latency_ms=3,
            model_latency_ms=4,
        )


@pytest.mark.asyncio
async def test_evaluation_run_records_versioned_provenance_without_passing_labels_to_answers() -> (
    None
):
    principal = Principal(uuid4(), uuid4(), "member@example.test", UserRole.MEMBER, uuid4(), "csrf")
    knowledge_base_id = uuid4()
    document_id = uuid4()
    chunk = RetrievedChunk(
        chunk_id=uuid4(),
        stable_id="refund-policy-v1:0",
        document_version_id=uuid4(),
        document_id=document_id,
        organization_id=principal.organization_id,
        knowledge_base_id=knowledge_base_id,
        ordinal=0,
        text="Refunds take five business days.",
        page_number=2,
        section="Eligibility",
        resource_authorized=True,
        title="Refund policy",
    )
    case = EvaluationCase(
        case_id="regression-1",
        question="How long do refunds take?",
        answerable=True,
        authoritative_document_ids=[document_id],
        expected_claims=["Refunds take five business days."],
        forbidden_document_ids=[uuid4()],
        tags=["refund"],
    )
    dataset = EvaluationDataset(
        kind=EvaluationDatasetKind.REGRESSION,
        version="rag-regression-v1",
        cases=[case],
    )
    service = SingleRetrievalAnswerService(
        chunk,
        SourceCitation(
            chunk_id=chunk.chunk_id,
            document_version_id=chunk.document_version_id,
            title=chunk.title,
            section=chunk.section,
            page_number=chunk.page_number,
        ),
    )
    provenance = EvaluationProvenance(
        document_version_set="documents-2026-08-24",
        chunking_version="chunking-v1",
        embedding_model="fake-embedding-v1",
        retrieval_config={"rrf_k": "60", "limit": "10"},
        prompt_version="grounded-answer-v1",
        llm_model="fake-evaluation-model",
    )

    run = await RAGEvaluationRunner(service).run(
        principal,
        knowledge_base_id,
        dataset,
        provenance,
    )

    assert run.dataset_version == "rag-regression-v1"
    assert run.document_version_set == "documents-2026-08-24"
    assert run.prompt_version == "grounded-answer-v1"
    assert run.metrics.recall_at_10 == 1.0
    assert run.metrics.citation_mapping_rate == 1.0
    assert run.metrics.citation_support_rate == 1.0
    assert run.metrics.claim_groundedness == 1.0
    assert service.questions == ["How long do refunds take?"]


@pytest.mark.asyncio
async def test_evaluation_uses_the_exact_retrieval_evidence_from_the_answer_service() -> None:
    principal = Principal(uuid4(), uuid4(), "member@example.test", UserRole.MEMBER, uuid4(), "csrf")
    knowledge_base_id = uuid4()
    chunk = RetrievedChunk(
        chunk_id=uuid4(),
        stable_id="refund-policy-v1:0",
        document_version_id=uuid4(),
        document_id=uuid4(),
        organization_id=principal.organization_id,
        knowledge_base_id=knowledge_base_id,
        ordinal=0,
        text="Refunds take five business days.",
        page_number=2,
        section="Eligibility",
        resource_authorized=True,
        title="Refund policy",
    )
    dataset = EvaluationDataset(
        kind=EvaluationDatasetKind.REGRESSION,
        version="rag-regression-v1",
        cases=[
            EvaluationCase(
                case_id="same-evidence",
                question="How long do refunds take?",
                answerable=True,
                authoritative_document_ids=[chunk.document_id],
                expected_claims=["Refunds take five business days."],
                forbidden_document_ids=[],
                tags=["regression"],
            )
        ],
    )
    service = SingleRetrievalAnswerService(
        chunk,
        SourceCitation(
            chunk_id=chunk.chunk_id,
            document_version_id=chunk.document_version_id,
            title=chunk.title,
            section=chunk.section,
            page_number=chunk.page_number,
        ),
    )
    provenance = EvaluationProvenance(
        document_version_set="documents-v1",
        chunking_version="chunking-v1",
        embedding_model="embedding-v1",
        retrieval_config={"limit": "10"},
        prompt_version="grounded-answer-v1",
        llm_model="fake",
    )

    run = await RAGEvaluationRunner(service).run(principal, knowledge_base_id, dataset, provenance)

    assert service.evidence_calls == 1
    assert run.metrics.retrieval_latency_ms == 3
    assert run.metrics.model_latency_ms == 4
