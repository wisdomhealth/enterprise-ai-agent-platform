from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.email.drafting import EmailDraftingService
from app.modules.email.models import EmailDraftVersion, EmailState
from app.modules.email.review import EmailReviewService
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.answer_service import AnswerExecution
from app.modules.rag.types import ClaimSupport, RetrievedChunk, SourceCitation, ValidatedAnswer


class RegeneratedAnswer:
    def __init__(self, item) -> None:  # type: ignore[no-untyped-def]
        chunk_id = uuid4()
        document_version_id = uuid4()
        self.execution = AnswerExecution(
            ValidatedAnswer(
                text="Short grounded reply.",
                claims=[ClaimSupport(text="Short grounded reply.", citation_ids=[chunk_id])],
                citations=[
                    SourceCitation(
                        chunk_id=chunk_id,
                        document_version_id=document_version_id,
                        title="Policy",
                        section="Replies",
                        page_number=1,
                    )
                ],
                segments=["Short grounded reply."],
                refused=False,
                model="claude-regenerate",
                prompt_version="grounded-answer-v1",
                latency_ms=4,
                input_tokens=12,
                output_tokens=4,
                estimated_cost=0.001,
            ),
            [
                RetrievedChunk(
                    chunk_id=chunk_id,
                    stable_id=str(chunk_id),
                    document_version_id=document_version_id,
                    document_id=uuid4(),
                    organization_id=item.organization_id,
                    knowledge_base_id=item.knowledge_base_id,
                    ordinal=0,
                    text="Short replies are permitted.",
                    page_number=1,
                    section="Replies",
                    resource_authorized=True,
                    title="Policy",
                )
            ],
            retrieval_latency_ms=1,
            model_latency_ms=3,
        )

    async def answer_with_evidence(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return self.execution


@pytest.mark.asyncio
async def test_regeneration_preserves_prior_versions_and_safe_events(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    first = email_review_context["draft"]
    principal = email_review_context["principal"]
    drafting = EmailDraftingService(db_session, RegeneratedAnswer(item), principal)
    service = EmailReviewService(db_session, principal, drafting_service=drafting)

    result = await service.regenerate(
        item.id,
        instruction="Use a shorter tone.",
        expected_version=item.version,
        current_draft_id=first.id,
    )

    versions = list(
        (
            await db_session.scalars(
                select(EmailDraftVersion)
                .where(EmailDraftVersion.work_item_id == item.id)
                .order_by(EmailDraftVersion.version)
            )
        ).all()
    )
    assert result.state is EmailState.AWAITING_REVIEW
    assert result.current_draft_id != first.id
    assert [version.version for version in versions] == [1, 2]
    assert versions[0].body == "Original grounded draft."
    assert versions[1].body == "Short grounded reply."
    assert versions[1].reviewer_instruction == "Use a shorter tone."
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.object_id == item.id,
            AuditEvent.action == "email.draft.regenerate",
        )
    )
    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == item.id,
            OutboxEvent.event_type == "email.draft.regenerated",
        )
    )
    assert audit is not None and "body" not in audit.details
    assert event is not None and "body" not in event.payload
    assert "Short grounded reply." not in str(event.payload)


@pytest.mark.asyncio
async def test_editing_approved_body_creates_version_and_invalidates_approval(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    service = EmailReviewService(db_session, email_review_context["principal"])
    approved = await service.approve(
        item.id,
        expected_version=item.version,
        current_draft_id=draft.id,
    )

    edited = await service.edit(
        item.id,
        body="Changed by reviewer.",
        expected_version=approved.version,
        current_draft_id=approved.current_draft_id,
    )

    assert edited.state is EmailState.AWAITING_REVIEW
    assert edited.approval is not None
    assert edited.approval.invalidated_at is not None
    assert edited.current_draft_id != draft.id
    versions = list(
        (
            await db_session.scalars(
                select(EmailDraftVersion).where(EmailDraftVersion.work_item_id == item.id)
            )
        ).all()
    )
    assert len(versions) == 2
    assert {version.body for version in versions} == {
        "Original grounded draft.",
        "Changed by reviewer.",
    }
