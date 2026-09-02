"""Test-only Task 16/20 browser harness; never use this ASGI app in production.

It exposes fixture credentials under ``/__e2e__`` while exercising the real
support router, service, and PostgreSQL lifecycle.
"""

# The fail-closed guard must run before imports that initialize the database or app.
# ruff: noqa: E402

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from tests.task16_e2e_guard import validate_task16_e2e_environment

validate_task16_e2e_environment(os.environ)

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import async_sessionmaker
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.email.models import (
    DeliveryAttempt,
    DeliveryIntent,
    EmailAction,
    EmailCategory,
    EmailDraftVersion,
    EmailPriority,
    EmailState,
    EmailStateHistory,
    EmailWorkItem,
)
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.identity.models import (
    Organization,
    StaffSession,
    StaffUser,
    UserRole,
    UserStatus,
)
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.answer_service import AnswerExecution
from app.modules.rag.types import (
    AnswerAudience,
    ClaimSupport,
    RetrievedChunk,
    SourceCitation,
    ValidatedAnswer,
)
from app.modules.support.models import Handoff, HandoffTrigger
from app.modules.support.service import SupportService

CSRF_TOKEN = "task16-browser-csrf"
app = create_app(Settings.model_validate({"SESSION_SECRET": "task16-browser-secret"}))
fixture: dict[str, UUID] = {}


class CountingAnswerService:
    def __init__(self) -> None:
        self.calls = 0

    async def answer(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> ValidatedAnswer:
        del principal, knowledge_base_id, query, audience
        self.calls += 1
        raise AssertionError("a stale answer job must not reach the model boundary")


class BrowserGroundedAnswerService:
    """Deterministic provider-free grounded response for the email browser fixture."""

    async def answer_with_evidence(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> AnswerExecution:
        del query
        assert audience is AnswerAudience.STAFF
        chunk_id = uuid4()
        document_version_id = uuid4()
        answer = "Regenerated grounded reply."
        return AnswerExecution(
            ValidatedAnswer(
                text=answer,
                claims=[ClaimSupport(text=answer, citation_ids=[chunk_id])],
                citations=[
                    SourceCitation(
                        chunk_id=chunk_id,
                        document_version_id=document_version_id,
                        title="Support policy",
                        section="Response times",
                        page_number=2,
                        internal_drive_link="https://drive.google.com/open?id=task20-reference",
                    )
                ],
                segments=[answer],
                refused=False,
                model="claude-e2e",
                prompt_version="email-e2e-v1",
                latency_ms=3,
                input_tokens=10,
                output_tokens=4,
                estimated_cost=0.0,
            ),
            [
                RetrievedChunk(
                    chunk_id=chunk_id,
                    stable_id=str(chunk_id),
                    document_version_id=document_version_id,
                    document_id=uuid4(),
                    organization_id=principal.organization_id,
                    knowledge_base_id=knowledge_base_id,
                    ordinal=0,
                    text="Support responds within one business day.",
                    page_number=2,
                    section="Response times",
                    resource_authorized=True,
                    title="Support policy",
                    internal_drive_link="https://drive.google.com/open?id=task20-reference",
                )
            ],
            retrieval_latency_ms=1,
            model_latency_ms=2,
        )


@app.on_event("startup")
async def seed_task16_lifecycle() -> None:
    async with async_sessionmaker() as db_session:
        organization = Organization(name=f"Task 16 browser {uuid4()}")
        db_session.add(organization)
        await db_session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id, public_key=f"task16-browser-{uuid4().hex}"
        )
        connector_secret = ConnectorSecret(
            organization_id=organization.id,
            ciphertext=b"task20-fixture-ciphertext",
            encrypted_data_key=b"task20-fixture-key",
            nonce=b"task20-fixture-nonce",
            algorithm="AES-256-GCM",
            key_version="fixture",
        )
        db_session.add_all((knowledge_base, connector_secret))
        await db_session.flush()
        gmail_connector = Connector(
            organization_id=organization.id,
            kind=ConnectorKind.GMAIL,
            status=ConnectorStatus.ACTIVE,
            secret_id=connector_secret.id,
        )
        db_session.add(gmail_connector)
        await db_session.flush()
        session = ChatSession(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            customer_name="Ada",
            customer_email="ada@example.test",
        )
        db_session.add(session)
        await db_session.flush()
        customer = ChatMessage(
            session_id=session.id,
            sequence=1,
            actor=ChatActor.CUSTOMER,
            body="I need a person",
        )
        db_session.add(customer)
        await db_session.flush()
        stale_job = JobIntent(
            kind="chat.answer",
            idempotency_key=f"task16-browser-{uuid4()}",
            payload={"session_id": str(session.id), "message_id": str(customer.id)},
            state=JobState.PENDING,
        )
        reviewer = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"task16-browser-{uuid4()}",
            email="reviewer@example.test",
            role=UserRole.REVIEWER,
            status=UserStatus.ACTIVE,
        )
        db_session.add_all([stale_job, reviewer])
        await db_session.flush()
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=reviewer.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.review"],
            )
        )
        staff_session = StaffSession(
            user_id=reviewer.id,
            csrf_hash=sha256(CSRF_TOKEN.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add(staff_session)
        await db_session.flush()
        email_item = EmailWorkItem(
            organization_id=organization.id,
            connector_id=gmail_connector.id,
            knowledge_base_id=knowledge_base.id,
            gmail_message_id=f"task20-browser-{uuid4().hex}",
            gmail_thread_id="task20-browser-thread",
            sender="customer@example.test",
            recipients=["support@example.test"],
            subject="Browser review request",
            body="Please confirm the support response time.",
            received_at=datetime.now(UTC),
            raw_content_ref="gmail://task20/browser-fixture",
            state=EmailState.AWAITING_REVIEW,
            category=EmailCategory.ACTION_REQUIRED,
            priority=EmailPriority.HIGH,
            reply_required=True,
            draft_body="Initial grounded reply.",
            draft_citations=[],
            draft_provenance={
                "model": "claude-e2e",
                "prompt_version": "email-e2e-v1",
            },
            version=3,
        )
        db_session.add(email_item)
        await db_session.flush()
        email_draft = EmailDraftVersion(
            work_item_id=email_item.id,
            organization_id=organization.id,
            version=1,
            body="Initial grounded reply.",
            to=["customer@example.test"],
            cc=[],
            subject="Re: Browser review request",
            thread_id=email_item.gmail_thread_id,
            reviewer_instruction=None,
            model="claude-e2e",
            prompt_version="email-e2e-v1",
            retrieval_config={},
            citations=[],
            created_by_id=reviewer.id,
            creator_type="STAFF",
        )
        db_session.add(email_draft)
        await db_session.flush()
        email_item.current_draft_id = email_draft.id
        db_session.add(
            EmailStateHistory(
                work_item_id=email_item.id,
                organization_id=organization.id,
                from_state=EmailState.DRAFTING,
                to_state=EmailState.AWAITING_REVIEW,
                action=EmailAction.DRAFT_READY,
                actor_id=reviewer.id,
                actor_type="STAFF",
                resource_version=email_item.version,
            )
        )
        handoff = await SupportService(db_session).request_handoff(
            session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST
        )
        await db_session.commit()
        fixture.update(
            {
                "staff_session_id": staff_session.id,
                "handoff_id": handoff.id,
                "job_id": stale_job.id,
                "session_id": session.id,
                "email_id": email_item.id,
            }
        )
        app.state.grounded_answer_service = BrowserGroundedAnswerService()


@app.get("/__e2e__/fixture")
async def fixture_read() -> dict[str, object]:
    return {
        "staff_session_id": str(fixture["staff_session_id"]),
        "csrf_token": CSRF_TOKEN,
        "handoff_id": str(fixture["handoff_id"]),
        "email_id": str(fixture["email_id"]),
    }


@app.get("/__e2e__/state")
async def fixture_state(
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    del principal
    handoff = await db_session.get(Handoff, fixture["handoff_id"])
    job = await db_session.get(JobIntent, fixture["job_id"])
    output_count = await db_session.scalar(
        select(func.count(ChatMessage.id)).where(
            ChatMessage.session_id == fixture["session_id"],
            ChatMessage.actor.in_([ChatActor.AI, ChatActor.SYSTEM]),
        )
    )
    answer_event_count = await db_session.scalar(
        select(func.count(OutboxEvent.event_id)).where(
            OutboxEvent.aggregate_id == fixture["session_id"],
            OutboxEvent.event_type.like("chat.answer.%"),
        )
    )
    assert handoff is not None and job is not None
    return {
        "handoff_state": handoff.state.value,
        "handoff_version": handoff.version,
        "job_state": job.state.value,
        "job_error": job.last_error_code,
        "output_count": output_count,
        "answer_event_count": answer_event_count,
    }


@app.post("/__e2e__/attempt-stale-answer")
async def attempt_stale_answer(
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    del principal
    answer_service = CountingAnswerService()
    result = await ChatAnswerService(db_session, answer_service).process(fixture["job_id"])
    return {"published": result is not None, "model_calls": answer_service.calls}


@app.post("/__e2e__/email-delivery-unknown")
async def make_email_delivery_unknown(
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    item = await db_session.get(EmailWorkItem, fixture["email_id"])
    assert item is not None and item.organization_id == principal.organization_id
    intent = await db_session.scalar(
        select(DeliveryIntent).where(
            DeliveryIntent.work_item_id == item.id,
            DeliveryIntent.organization_id == principal.organization_id,
            DeliveryIntent.approved_draft_version_id == item.current_draft_id,
        )
    )
    assert intent is not None
    previous = item.state
    item.state = EmailState.DELIVERY_UNKNOWN
    item.version += 1
    intent.state = EmailState.DELIVERY_UNKNOWN
    intent.version += 1
    intent.last_error_code = "GMAIL_RESPONSE_TIMEOUT"
    db_session.add_all(
        [
            DeliveryAttempt(
                delivery_intent_id=intent.id,
                attempt_number=1,
                outcome="UNKNOWN",
                error_code="GMAIL_RESPONSE_TIMEOUT",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            ),
            EmailStateHistory(
                work_item_id=item.id,
                organization_id=item.organization_id,
                from_state=previous,
                to_state=item.state,
                action=EmailAction.DELIVERY_AMBIGUOUS,
                reason_code="GMAIL_RESPONSE_TIMEOUT",
                actor_type="SYSTEM",
                resource_version=item.version,
            ),
        ]
    )
    await db_session.commit()
    return {"state": item.state.value, "delivery_intent_id": str(intent.id)}
