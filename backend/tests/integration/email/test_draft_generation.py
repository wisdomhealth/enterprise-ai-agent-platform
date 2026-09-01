from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.email.actors import email_worker_principal
from app.modules.email.classification import ClassificationExecution
from app.modules.email.drafting import EmailDraftingService
from app.modules.email.ingestion import EmailIngestionService
from app.modules.email.models import (
    EmailCategory,
    EmailPriority,
    EmailState,
    EmailStateHistory,
)
from app.modules.email.schemas import EmailClassification
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.service import JobService
from app.modules.rag.answer_service import AnswerExecution
from app.modules.rag.types import (
    AnswerAudience,
    ClaimSupport,
    RetrievedChunk,
    SourceCitation,
    ValidatedAnswer,
)


class FixedClassifier:
    async def classify(self, _subject: str, _body: str) -> ClassificationExecution:
        return ClassificationExecution(
            EmailClassification(
                category=EmailCategory.ACTION_REQUIRED,
                priority=EmailPriority.HIGH,
                reply_required=True,
            ),
            "claude-fixture",
            "email-classification-v1",
            3,
            10,
            4,
            0.00009,
        )


class FakeGroundedAnswerService:
    def __init__(self, execution: AnswerExecution) -> None:
        self.execution = execution
        self.audiences: list[AnswerAudience] = []

    async def answer_with_evidence(
        self,
        _principal,
        _knowledge_base_id,
        _query,
        audience,  # type: ignore[no-untyped-def]
    ) -> AnswerExecution:
        self.audiences.append(audience)
        return self.execution


class FailingOutboxService:
    async def add(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("outbox unavailable")


@pytest.mark.asyncio
async def test_draft_contains_only_authorized_staff_citations(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    item = await EmailIngestionService(db_session, classifier=FixedClassifier()).ingest_message(
        email_context["message"],
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )
    chunk_id = uuid4()
    version_id = uuid4()
    citation = SourceCitation(
        chunk_id=chunk_id,
        document_version_id=version_id,
        title="Refund policy",
        section="Timing",
        page_number=3,
        internal_drive_link="https://drive.google.com/private",
    )
    answer = ValidatedAnswer(
        text="We can help with your refund request.",
        claims=[ClaimSupport(text="We can help.", citation_ids=[chunk_id])],
        citations=[citation],
        segments=["We can help with your refund request."],
        refused=False,
        model="claude-draft",
        prompt_version="grounded-answer-v1",
        latency_ms=9,
        input_tokens=21,
        output_tokens=8,
        estimated_cost=0.0002,
    )
    chunk = RetrievedChunk(
        chunk_id=chunk_id,
        stable_id=str(chunk_id),
        document_version_id=version_id,
        document_id=uuid4(),
        organization_id=item.organization_id,
        knowledge_base_id=item.knowledge_base_id,
        ordinal=0,
        text="Refund requests are handled by support.",
        page_number=3,
        section="Timing",
        resource_authorized=True,
        title="Refund policy",
        internal_drive_link="https://drive.google.com/private",
    )
    grounded = FakeGroundedAnswerService(
        AnswerExecution(answer, [chunk], retrieval_latency_ms=2, model_latency_ms=7)
    )
    job = await JobService().enqueue(
        db_session,
        "email.draft",
        f"draft-provenance-{uuid4()}",
        {"work_item_id": str(item.id)},
    )
    principal = email_worker_principal(item.organization_id, item.knowledge_base_id, job.id)

    draft = await EmailDraftingService(db_session, grounded, principal).generate(
        item.id, job_id=job.id
    )

    assert draft.state is EmailState.AWAITING_REVIEW
    assert draft.citations
    assert all(c.organization_id == item.organization_id for c in draft.citations)
    assert grounded.audiences == [AnswerAudience.STAFF]
    assert draft.provenance.model == "claude-draft"
    assert draft.provenance.retrieval_latency_ms == 2
    assert draft.provenance.retrieval_actor_type == "SYSTEM"
    history = await db_session.scalar(
        select(EmailStateHistory).where(
            EmailStateHistory.work_item_id == item.id,
            EmailStateHistory.action == "DRAFT_READY",
        )
    )
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.object_id == item.id,
            AuditEvent.action == "email.draft.generate",
        )
    )
    assert history is not None
    assert history.job_id == job.id
    assert history.actor_type == "SYSTEM"
    assert history.actor_id == principal.subject_id
    assert audit is not None
    assert audit.actor_id == principal.subject_id
    assert audit.details["actor_type"] == "SYSTEM"
    assert audit.details["job_id"] == str(job.id)


@pytest.mark.asyncio
async def test_draft_rejects_cross_organization_retrieval_evidence(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    item = await EmailIngestionService(db_session, classifier=FixedClassifier()).ingest_message(
        email_context["message"],
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )
    chunk_id = uuid4()
    version_id = uuid4()
    answer = ValidatedAnswer(
        text="Unsafe draft",
        claims=[],
        citations=[
            SourceCitation(
                chunk_id=chunk_id,
                document_version_id=version_id,
                title="Other organization",
                section=None,
                page_number=None,
            )
        ],
        segments=["Unsafe draft"],
        refused=False,
        model="fake",
        prompt_version="test",
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        estimated_cost=0,
    )
    other_chunk = RetrievedChunk(
        chunk_id=chunk_id,
        stable_id=str(chunk_id),
        document_version_id=version_id,
        document_id=uuid4(),
        organization_id=uuid4(),
        knowledge_base_id=item.knowledge_base_id,
        ordinal=0,
        text="No",
        page_number=None,
        section=None,
        resource_authorized=True,
    )
    principal = Principal(
        email_context["staff_user"].id,
        item.organization_id,
        "reviewer@example.test",
        UserRole.REVIEWER,
        uuid4(),
        "x",
    )

    draft = await EmailDraftingService(
        db_session,
        FakeGroundedAnswerService(AnswerExecution(answer, [other_chunk], 0, 0)),
        principal,
    ).generate(item.id)

    assert draft.state is EmailState.DRAFT_RETRY_WAIT
    assert draft.error_code == "EMAIL_DRAFT_UNAUTHORIZED_CITATION"
    assert draft.body is None


@pytest.mark.asyncio
async def test_draft_persistence_failure_rolls_back_instead_of_becoming_model_failure(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    item = await EmailIngestionService(db_session, classifier=FixedClassifier()).ingest_message(
        email_context["message"],
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )
    await db_session.commit()
    chunk_id = uuid4()
    version_id = uuid4()
    answer = ValidatedAnswer(
        text="Grounded draft.",
        claims=[ClaimSupport(text="Grounded draft.", citation_ids=[chunk_id])],
        citations=[
            SourceCitation(
                chunk_id=chunk_id,
                document_version_id=version_id,
                title="Policy",
                section=None,
                page_number=None,
            )
        ],
        segments=["Grounded draft."],
        refused=False,
        model="fake",
        prompt_version="test",
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        estimated_cost=0,
    )
    chunk = RetrievedChunk(
        chunk_id=chunk_id,
        stable_id=str(chunk_id),
        document_version_id=version_id,
        document_id=uuid4(),
        organization_id=item.organization_id,
        knowledge_base_id=item.knowledge_base_id,
        ordinal=0,
        text="Grounded draft.",
        page_number=None,
        section=None,
        resource_authorized=True,
    )
    principal = Principal(
        email_context["staff_user"].id,
        item.organization_id,
        "reviewer@example.test",
        UserRole.REVIEWER,
        uuid4(),
        "x",
    )

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await EmailDraftingService(
            db_session,
            FakeGroundedAnswerService(AnswerExecution(answer, [chunk], 0, 1)),
            principal,
            outbox_service=FailingOutboxService(),
        ).generate(item.id)
    await db_session.rollback()
    await db_session.refresh(item)

    assert item.state is EmailState.DRAFTING
    assert item.last_error_code is None
