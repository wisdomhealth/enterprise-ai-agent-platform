"""Test-only Task 16 browser harness; never use this ASGI app in production.

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
from app.modules.rag.types import AnswerAudience, ValidatedAnswer
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


@app.on_event("startup")
async def seed_task16_lifecycle() -> None:
    async with async_sessionmaker() as db_session:
        organization = Organization(name=f"Task 16 browser {uuid4()}")
        db_session.add(organization)
        await db_session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id, public_key=f"task16-browser-{uuid4().hex}"
        )
        db_session.add(knowledge_base)
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
            }
        )


@app.get("/__e2e__/fixture")
async def fixture_read() -> dict[str, object]:
    return {
        "staff_session_id": str(fixture["staff_session_id"]),
        "csrf_token": CSRF_TOKEN,
        "handoff_id": str(fixture["handoff_id"]),
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
