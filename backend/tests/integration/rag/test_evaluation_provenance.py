import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from app.core.database import async_sessionmaker, engine
from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.rag.answer_service import AnswerExecution, GroundedAnswerService
from app.modules.rag.evaluation import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationDatasetKind,
    EvaluationProvenance,
    EvaluationRunRepository,
    RAGEvaluationRunner,
)
from app.modules.rag.evaluation_models import RAGEvaluationCase
from app.modules.rag.groundedness import CitationValidator
from app.modules.rag.llm import GeneratedAnswer, InMemoryRedisCircuitStore, ProviderCircuitBreaker
from app.modules.rag.retriever import HybridRetriever
from app.modules.rag.text_search import TextCandidateSource as PostgreSQLTextCandidateSource
from app.modules.rag.types import (
    AnswerAudience,
    ClaimSupport,
    RetrievedChunk,
    SourceCitation,
    ValidatedAnswer,
)
from app.modules.rag.vector_search import VectorCandidateSource as PostgreSQLVectorCandidateSource


class StaticEmbeddingProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 1536 for _ in texts]


class ContextClaimProvider:
    """Deterministic fake for the external provider; retrieval remains production code."""

    async def generate(self, prompt: object) -> GeneratedAnswer:
        user_message = getattr(prompt, "user_message")
        chunk_id = UUID(user_message.split('"chunk_id":"', 1)[1].split('"', 1)[0])
        text = "Refunds take five business days."
        return GeneratedAnswer(
            text=text,
            claims=[ClaimSupport(text=text, citation_ids=[chunk_id])],
            model="test-provider",
            input_tokens=3,
            output_tokens=4,
        )


class CountingHybridRetriever(HybridRetriever):
    calls = 0

    async def retrieve(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
    ) -> list[RetrievedChunk]:
        self.calls += 1
        return await super().retrieve(principal, knowledge_base_id, query, limit)


class RecordingVectorSource(PostgreSQLVectorCandidateSource):
    def __init__(self, barrier: asyncio.Barrier) -> None:
        super().__init__(async_sessionmaker)
        self._barrier = barrier
        self.backend_pids: list[int] = []
        self.results: list[list[RetrievedChunk]] = []

    async def _search_with_session(self, db_session, *args, **kwargs):  # type: ignore[no-untyped-def]
        backend_pid = await db_session.scalar(select(func.pg_backend_pid()))
        assert isinstance(backend_pid, int)
        self.backend_pids.append(backend_pid)
        await asyncio.wait_for(self._barrier.wait(), timeout=2)
        result = await super()._search_with_session(db_session, *args, **kwargs)
        self.results.append(result)
        return result


class RecordingTextSource(PostgreSQLTextCandidateSource):
    def __init__(self, barrier: asyncio.Barrier) -> None:
        super().__init__(async_sessionmaker)
        self._barrier = barrier
        self.backend_pids: list[int] = []
        self.results: list[list[RetrievedChunk]] = []

    async def _search_with_session(self, db_session, *args, **kwargs):  # type: ignore[no-untyped-def]
        backend_pid = await db_session.scalar(select(func.pg_backend_pid()))
        assert isinstance(backend_pid, int)
        self.backend_pids.append(backend_pid)
        await asyncio.wait_for(self._barrier.wait(), timeout=2)
        result = await super()._search_with_session(db_session, *args, **kwargs)
        self.results.append(result)
        return result


@dataclass(frozen=True)
class HistoricalEvidenceFixture:
    organization_id: UUID
    principal: Principal
    knowledge_base_id: UUID
    document_id: UUID
    version_a_id: UUID
    chunk_a_id: UUID
    chunk_a_ids: tuple[UUID, UUID]


async def _seed_historical_evidence() -> HistoricalEvidenceFixture:
    async with async_sessionmaker() as db_session:
        organization = Organization(name=f"evaluation history {uuid4()}")
        db_session.add(organization)
        await db_session.flush()
        knowledge_base = KnowledgeBase(organization_id=organization.id)
        db_session.add(knowledge_base)
        user = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"evaluator-{uuid4()}",
            email="evaluator@example.test",
            role=UserRole.REVIEWER,
            status=UserStatus.ACTIVE,
        )
        db_session.add(user)
        await db_session.flush()
        staff_session = StaffSession(
            user_id=user.id,
            csrf_hash="test",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        db_session.add(staff_session)
        source = DriveSource(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            root_folder_id="root",
            connection_identity="reader@example.test",
        )
        db_session.add(source)
        await db_session.flush()
        document = Document(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            source_id=source.id,
            external_id=str(uuid4()),
            title="Refund policy",
            mime_type="application/pdf",
        )
        db_session.add(document)
        await db_session.flush()
        version_a = DocumentVersion(
            document_id=document.id,
            state=DocumentVersionState.RETRIEVABLE,
            content_sha256="a" * 64,
        )
        db_session.add(version_a)
        await db_session.flush()
        document.current_version_id = version_a.id
        chunk_a_id, secondary_chunk_a_id = sorted((uuid4(), uuid4()))
        db_session.add_all(
            [
                DocumentChunk(
                    id=chunk_a_id,
                    document_version_id=version_a.id,
                    ordinal=0,
                    text="Refunds take five business days.",
                    page_number=2,
                    section="Eligibility",
                    token_count=5,
                    metadata_={},
                    embedding=[1.0] * 1536,
                ),
                DocumentChunk(
                    id=secondary_chunk_a_id,
                    document_version_id=version_a.id,
                    ordinal=1,
                    text="Refunds take five business days for eligible returns.",
                    page_number=3,
                    section="Details",
                    token_count=8,
                    metadata_={},
                    embedding=[1.0] * 1536,
                ),
            ]
        )
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=user.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.read"],
            )
        )
        await db_session.commit()
        return HistoricalEvidenceFixture(
            organization_id=organization.id,
            principal=Principal(
                user.id,
                organization.id,
                user.email,
                user.role,
                staff_session.id,
                "test",
            ),
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            version_a_id=version_a.id,
            chunk_a_id=chunk_a_id,
            chunk_a_ids=(chunk_a_id, secondary_chunk_a_id),
        )


class CorpusChangingAnswerService:
    """Publishes corpus version B only after Task 11 returned evidence from version A."""

    def __init__(self, answer_service: GroundedAnswerService, document_id: UUID) -> None:
        self._answer_service = answer_service
        self._document_id = document_id
        self.calls = 0
        self.version_b_id: UUID | None = None
        self.chunk_b_ids: tuple[UUID, UUID] | None = None

    async def answer_with_evidence(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> AnswerExecution:
        self.calls += 1
        execution = await self._answer_service.answer_with_evidence(
            principal, knowledge_base_id, query, audience
        )
        async with async_sessionmaker() as db_session:
            document = await db_session.get(Document, self._document_id)
            assert document is not None
            version_b = DocumentVersion(
                document_id=document.id,
                state=DocumentVersionState.RETRIEVABLE,
                content_sha256="b" * 64,
            )
            db_session.add(version_b)
            await db_session.flush()
            document.current_version_id = version_b.id
            chunk_b_id, secondary_chunk_b_id = sorted((uuid4(), uuid4()))
            db_session.add_all(
                [
                    DocumentChunk(
                        id=chunk_b_id,
                        document_version_id=version_b.id,
                        ordinal=0,
                        text="Refunds take ten business days.",
                        page_number=2,
                        section="Eligibility",
                        token_count=5,
                        metadata_={},
                        embedding=[1.0] * 1536,
                    ),
                    DocumentChunk(
                        id=secondary_chunk_b_id,
                        document_version_id=version_b.id,
                        ordinal=1,
                        text="Refunds take ten business days for eligible returns.",
                        page_number=3,
                        section="Details",
                        token_count=8,
                        metadata_={},
                        embedding=[1.0] * 1536,
                    ),
                ]
            )
            await db_session.commit()
            self.version_b_id = version_b.id
            self.chunk_b_ids = (chunk_b_id, secondary_chunk_b_id)
        return execution


async def _delete_organization(organization_id: UUID) -> None:
    async with async_sessionmaker() as db_session:
        await db_session.execute(delete(Organization).where(Organization.id == organization_id))
        await db_session.commit()


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


class RecordingEvidenceAnswerService(RecordingAnswerService):
    """Label-boundary test double; the separate same-evidence test uses real Hybrid retrieval."""

    def __init__(self, chunk: RetrievedChunk, citation: SourceCitation) -> None:
        super().__init__(citation)
        self._chunk = chunk

    async def answer_with_evidence(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        question: str,
        audience: AnswerAudience,
    ) -> AnswerExecution:
        answer = await self.answer(principal, knowledge_base_id, question, audience)
        return AnswerExecution(
            answer=answer,
            retrieved_chunks=[self._chunk],
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
    service = RecordingEvidenceAnswerService(
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
async def test_evaluation_persists_task11_hybrid_evidence_from_history_a_after_corpus_changes() -> (
    None
):
    fixture = await _seed_historical_evidence()
    dataset = EvaluationDataset(
        kind=EvaluationDatasetKind.REGRESSION,
        version="rag-regression-v1",
        cases=[
            EvaluationCase(
                case_id="same-evidence",
                question="refunds take",
                answerable=True,
                authoritative_document_ids=[fixture.document_id],
                expected_claims=["Refunds take five business days."],
                forbidden_document_ids=[],
                tags=["regression"],
            )
        ],
    )
    barrier = asyncio.Barrier(2)
    vector_source = RecordingVectorSource(barrier)
    text_source = RecordingTextSource(barrier)
    retriever = CountingHybridRetriever(
        vector_source,
        text_source,
        StaticEmbeddingProvider(),
    )
    task11_service = GroundedAnswerService(
        retriever,
        ContextClaimProvider(),
        CitationValidator(),
        ProviderCircuitBreaker(InMemoryRedisCircuitStore()),
    )
    service = CorpusChangingAnswerService(task11_service, fixture.document_id)
    provenance = EvaluationProvenance(
        document_version_set="documents-v1",
        chunking_version="chunking-v1",
        embedding_model="embedding-v1",
        retrieval_config={"limit": "10"},
        prompt_version="grounded-answer-v1",
        llm_model="fake",
    )

    try:
        async with async_sessionmaker() as db_session:
            run = await RAGEvaluationRunner(
                service,
                EvaluationRunRepository(db_session),
            ).run(fixture.principal, fixture.knowledge_base_id, dataset, provenance)
            await db_session.commit()
            stored_case = await db_session.scalar(
                select(RAGEvaluationCase).where(RAGEvaluationCase.run_id == run.id)
            )

        assert service.calls == 1
        assert retriever.calls == 1
        assert run.metrics.recall_at_10 == 1.0
        assert run.metrics.citation_mapping_rate == 1.0
        assert run.metrics.citation_support_rate == 1.0
        assert run.metrics.answer_groundedness == 1.0
        assert run.metrics.claim_groundedness == 1.0
        assert len(vector_source.backend_pids) == len(text_source.backend_pids) == 1
        assert vector_source.backend_pids[0] != text_source.backend_pids[0]
        assert {item.chunk_id for item in vector_source.results[0]} == set(
            fixture.chunk_a_ids
        )
        assert {item.chunk_id for item in text_source.results[0]} == set(fixture.chunk_a_ids)
        assert all(
            item.document_version_id == fixture.version_a_id
            and item.resource_authorized
            and item.retrieval_eligible
            for item in vector_source.results[0] + text_source.results[0]
        )
        assert stored_case is not None
        assert set(stored_case.retrieved_chunk_ids) == set(fixture.chunk_a_ids)
        assert len(stored_case.retrieved_chunk_ids) == len(fixture.chunk_a_ids)
        assert stored_case.retrieved_document_version_ids == [
            fixture.version_a_id,
            fixture.version_a_id,
        ]
        assert stored_case.citation_chunk_ids == [fixture.chunk_a_id]
        assert stored_case.citation_document_version_ids == [fixture.version_a_id]
        assert stored_case.snapshot["result"]["text"] == "Refunds take five business days."
        assert stored_case.snapshot["result"]["citations"][0]["chunk_id"] == str(
            fixture.chunk_a_id
        )
        assert stored_case.snapshot["result"]["citations"][0][
            "document_version_id"
        ] == str(fixture.version_a_id)
        assert stored_case.snapshot["claims"] == [
            {"text": "Refunds take five business days.", "supported": True}
        ]

        assert service.version_b_id is not None
        assert service.chunk_b_ids is not None
        current_results = await retriever.retrieve(
            fixture.principal,
            fixture.knowledge_base_id,
            "refunds take",
            10,
        )
        assert retriever.calls == 2
        assert len(vector_source.backend_pids) == len(text_source.backend_pids) == 2
        assert vector_source.backend_pids[1] != text_source.backend_pids[1]
        assert {item.chunk_id for item in vector_source.results[1]} == set(
            service.chunk_b_ids
        )
        assert {item.chunk_id for item in text_source.results[1]} == set(service.chunk_b_ids)
        assert {item.chunk_id for item in current_results} == set(service.chunk_b_ids)
        assert len(current_results) == len(service.chunk_b_ids)
        assert all(
            item.document_version_id == service.version_b_id
            and item.chunk_id not in fixture.chunk_a_ids
            for item in current_results
        )

        async with async_sessionmaker() as verification_session:
            document = await verification_session.get(Document, fixture.document_id)
            assert document is not None
            assert document.current_version_id != fixture.version_a_id
            current_chunk = await verification_session.scalar(
                select(DocumentChunk).where(
                    DocumentChunk.document_version_id == document.current_version_id
                )
            )
        assert current_chunk is not None
        assert current_chunk.document_version_id == service.version_b_id
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()
