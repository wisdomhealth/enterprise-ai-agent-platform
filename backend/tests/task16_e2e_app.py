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
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import async_sessionmaker
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession
from app.modules.chat.tasks import _consume_chat_answer
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
from app.modules.email.review import EmailReviewService
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
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.types import AnswerAudience, ValidatedAnswer
from app.modules.support.models import Handoff, HandoffTrigger
from app.modules.support.service import SupportService

CSRF_TOKEN = "task16-browser-csrf"
app = create_app(
    Settings.model_validate(
        {
            "SESSION_SECRET": "task16-browser-secret",
            "ANTHROPIC_API_KEY": "task26-local",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:3201",
            "OPENAI_API_KEY": "task26-local",
            "OPENAI_BASE_URL": "http://127.0.0.1:3201/v1",
            "REDIS_URL": "redis://127.0.0.1:56385/0",
        }
    )
)
# The guarded browser harness calls the real local public API directly so the
# release gate exercises the same unbuffered SSE transport shape as Nginx.
# This middleware belongs only to the test ASGI application, never production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Accept"],
)
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


@app.on_event("startup")
async def seed_task16_lifecycle() -> None:
    async with async_sessionmaker() as db_session:
        organization = Organization(name=f"Task 16 browser {uuid4()}")
        db_session.add(organization)
        await db_session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id, public_key="task26-public-key"
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
        administrator = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"task26-admin-{uuid4()}",
            email="admin@example.test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        db_session.add_all([stale_job, reviewer, administrator])
        await db_session.flush()
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                    subject_id=reviewer.id,
                    resource_type="knowledge",
                    resource_id=knowledge_base.id,
                    # The real staff-draft regeneration path retrieves the
                    # review evidence under the same durable grant, so this
                    # fixture must authorize both review and read actions.
                    actions=["knowledge.read", "knowledge.review"],
            )
        )
        drive_source = DriveSource(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            root_folder_id="task26-root",
            connection_identity="task26@example.test",
        )
        db_session.add(drive_source)
        await db_session.flush()
        db_session.add_all(
            [
                ResourceGrant(
                    organization_id=organization.id,
                    subject_id=administrator.id,
                    resource_type="connector",
                    resource_id=gmail_connector.id,
                    actions=["connector.read"],
                ),
                ResourceGrant(
                    organization_id=organization.id,
                    subject_id=administrator.id,
                    resource_type="knowledge",
                    resource_id=knowledge_base.id,
                    actions=["knowledge.read", "knowledge.review"],
                ),
                ResourceGrant(
                    organization_id=organization.id,
                    subject_id=administrator.id,
                    resource_type="knowledge",
                    resource_id=KnowledgeSourceService.configuration_resource_id(organization.id),
                    actions=["knowledge.write"],
                ),
            ]
        )
        document = Document(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            source_id=drive_source.id,
            external_id="task26-policy",
            title="Support policy",
            mime_type="application/pdf",
        )
        db_session.add(document)
        await db_session.flush()
        version = DocumentVersion(
            document_id=document.id,
            state=DocumentVersionState.RETRIEVABLE,
            content_sha256="2" * 64,
        )
        db_session.add(version)
        await db_session.flush()
        document.current_version_id = version.id
        db_session.add(
            DocumentChunk(
                id=uuid4(),
                document_version_id=version.id,
                ordinal=0,
                text="Regenerated grounded reply.",
                page_number=2,
                section="Response times",
                token_count=3,
                metadata_={"task": 26},
                embedding=[1.0] * 1536,
            )
        )
        staff_session = StaffSession(
            user_id=reviewer.id,
            csrf_hash=sha256(CSRF_TOKEN.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        administrator_session = StaffSession(
            user_id=administrator.id,
            csrf_hash=sha256(CSRF_TOKEN.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add_all((staff_session, administrator_session))
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
        conflict_email_item = EmailWorkItem(
            organization_id=organization.id,
            connector_id=gmail_connector.id,
            knowledge_base_id=knowledge_base.id,
            gmail_message_id=f"task20-conflict-{uuid4().hex}",
            gmail_thread_id="task20-conflict-thread",
            sender="concurrent@example.test",
            recipients=["support@example.test"],
            subject="Concurrent browser review",
            body="Please review this email concurrently.",
            received_at=datetime.now(UTC),
            raw_content_ref="gmail://task20/concurrent-browser-fixture",
            state=EmailState.AWAITING_REVIEW,
            category=EmailCategory.ACTION_REQUIRED,
            priority=EmailPriority.NORMAL,
            reply_required=True,
            draft_body="Stale browser draft.",
            draft_citations=[],
            draft_provenance={
                "model": "claude-e2e",
                "prompt_version": "email-e2e-v1",
            },
            version=3,
        )
        db_session.add(conflict_email_item)
        await db_session.flush()
        conflict_email_draft = EmailDraftVersion(
            work_item_id=conflict_email_item.id,
            organization_id=organization.id,
            version=1,
            body="Stale browser draft.",
            to=["concurrent@example.test"],
            cc=[],
            subject="Re: Concurrent browser review",
            thread_id=conflict_email_item.gmail_thread_id,
            reviewer_instruction=None,
            model="claude-e2e",
            prompt_version="email-e2e-v1",
            retrieval_config={},
            citations=[],
            created_by_id=reviewer.id,
            creator_type="STAFF",
        )
        db_session.add(conflict_email_draft)
        await db_session.flush()
        conflict_email_item.current_draft_id = conflict_email_draft.id
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
                "admin_session_id": administrator_session.id,
                "knowledge_base_id": knowledge_base.id,
                "handoff_id": handoff.id,
                "job_id": stale_job.id,
                "session_id": session.id,
                "email_id": email_item.id,
                "conflict_email_id": conflict_email_item.id,
                "document_version_id": version.id,
            }
        )


@app.get("/__e2e__/fixture")
async def fixture_read() -> dict[str, object]:
    return {
        "staff_session_id": str(fixture["staff_session_id"]),
        "admin_session_id": str(fixture["admin_session_id"]),
        "csrf_token": CSRF_TOKEN,
        "handoff_id": str(fixture["handoff_id"]),
        "session_id": str(fixture["session_id"]),
        "email_id": str(fixture["email_id"]),
        "conflict_email_id": str(fixture["conflict_email_id"]),
    }


@app.get("/__e2e__/latest-public-session")
async def latest_public_session() -> dict[str, str]:
    async with async_sessionmaker() as db_session:
        session = await db_session.scalar(
            select(ChatSession)
            .where(ChatSession.knowledge_base_id == fixture["knowledge_base_id"])
            .order_by(ChatSession.created_at.desc())
            .limit(1)
        )
        if isinstance(session, ChatSession):
            fixture_principal = await db_session.get(StaffUser, session.id)
            if fixture_principal is None:
                # The production retrieval predicate grants only durable,
                # organization-bound subjects.  This test-only identity binds
                # the opaque public-session principal to an explicit persisted
                # fixture grant; it does not replace or bypass that predicate.
                db_session.add(
                    StaffUser(
                        id=session.id,
                        organization_id=session.organization_id,
                        oidc_subject=f"task26-public-{session.id}",
                        email=f"public-{session.id}@example.test",
                        role=UserRole.MEMBER,
                        status=UserStatus.ACTIVE,
                    )
                )
                await db_session.flush()
            db_session.add(
                ResourceGrant(
                    organization_id=session.organization_id,
                    subject_id=session.id,
                    resource_type="knowledge",
                    resource_id=session.knowledge_base_id,
                    actions=["knowledge.read"],
                )
            )
            await db_session.commit()
    if not isinstance(session, ChatSession):
        raise RuntimeError("public session fixture was not created")
    return {"session_id": str(session.id)}


@app.post("/__e2e__/consume-public-answer/{session_id}")
async def consume_public_answer(session_id: UUID) -> dict[str, str]:
    """Invoke the registered consumer assembly against the local fake provider.

    The route is only reachable in the fail-closed Task 16 harness.  It does
    not synthesize an answer: it finds the durable production intent created
    by the real public API and invokes the same consumer function Celery uses.
    """
    async with async_sessionmaker() as db_session:
        jobs = list(
            (
                await db_session.scalars(
                    select(JobIntent)
                    .where(JobIntent.kind == "chat.answer")
                    .order_by(JobIntent.created_at.desc())
                )
            ).all()
        )
        job = next(
            (
                candidate
                for candidate in jobs
                if candidate.payload.get("session_id") == str(session_id)
            ),
            None,
        )
    if job is None:
        raise RuntimeError("public answer intent was not created")
    await _consume_chat_answer(job.id)
    return {"job_id": str(job.id)}


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


@app.post("/__e2e__/email-concurrent-review")
async def make_concurrent_email_review(
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    item = await db_session.get(EmailWorkItem, fixture["conflict_email_id"])
    assert (
        item is not None
        and item.organization_id == principal.organization_id
        and item.current_draft_id is not None
    )
    service = EmailReviewService(db_session, principal)
    edited = await service.edit(
        item.id,
        expected_version=item.version,
        current_draft_id=item.current_draft_id,
        body="Concurrent durable draft.",
    )
    approved = await service.approve(
        item.id,
        expected_version=edited.version,
        current_draft_id=edited.current_draft_id,
    )
    intent = await db_session.scalar(
        select(DeliveryIntent).where(
            DeliveryIntent.work_item_id == item.id,
            DeliveryIntent.organization_id == principal.organization_id,
            DeliveryIntent.approved_draft_version_id == approved.current_draft_id,
        )
    )
    assert intent is not None
    previous = item.state
    item.state = EmailState.DELIVERY_UNKNOWN
    item.version += 1
    intent.state = EmailState.DELIVERY_UNKNOWN
    intent.version += 1
    intent.attempts = 2
    intent.last_error_code = "GMAIL_RESPONSE_TIMEOUT"
    db_session.add_all(
        [
            DeliveryAttempt(
                delivery_intent_id=intent.id,
                attempt_number=2,
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
    return {
        "state": item.state.value,
        "current_draft_id": str(item.current_draft_id),
        "delivery_intent_id": str(intent.id),
    }
