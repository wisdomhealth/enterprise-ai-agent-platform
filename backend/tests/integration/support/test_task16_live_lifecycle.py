from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.types import AnswerAudience, ValidatedAnswer
from app.modules.support.models import HandoffTrigger
from app.modules.support.service import SupportService

CSRF_TOKEN = "task16-live-csrf"


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


@asynccontextmanager
async def _client(
    db_session: AsyncSession, principal: Principal
) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(
        Settings.model_validate({"SESSION_SECRET": "task16-live-secret"})
    )

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_staff() -> Principal:
        return principal

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_staff_session] = override_staff
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_live_routes_claim_reply_resume_and_fence_preexisting_answer(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Task 16 lifecycle {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"task16-{uuid4().hex}"
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
        idempotency_key=f"task16-stale-{uuid4()}",
        payload={"session_id": str(session.id), "message_id": str(customer.id)},
        state=JobState.PENDING,
    )
    db_session.add(stale_job)
    reviewer = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"task16-{uuid4()}",
        email=f"reviewer-{uuid4()}@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(reviewer)
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
    await db_session.flush()
    handoff = await SupportService(db_session).request_handoff(
        session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST
    )
    await db_session.commit()
    handoff_id = handoff.id
    initial_version = handoff.version
    session_id = session.id
    stale_job_id = stale_job.id
    principal = Principal(
        reviewer.id,
        organization.id,
        reviewer.email,
        UserRole.REVIEWER,
        uuid4(),
        sha256(CSRF_TOKEN.encode()).hexdigest(),
    )
    headers = {"X-CSRF-Token": CSRF_TOKEN}

    async with _client(db_session, principal) as client:
        queue = await client.get("/api/v1/staff/support/queue")
        assert queue.status_code == 200
        assert [item["id"] for item in queue.json()] == [str(handoff_id)]

        claimed = await client.post(
            f"/api/v1/staff/support/{handoff_id}/claim",
            json={"version": initial_version},
            headers=headers,
        )
        assert claimed.status_code == 200
        assert claimed.json()["state"] == "HUMAN_ACTIVE"

        stale_claim = await client.post(
            f"/api/v1/staff/support/{handoff_id}/claim",
            json={"version": initial_version},
            headers=headers,
        )
        assert stale_claim.status_code == 409

        reply = await client.post(
            f"/api/v1/staff/support/{handoff_id}/reply",
            json={"version": claimed.json()["version"], "body": "A human reply"},
            headers=headers,
        )
        assert reply.status_code == 200
        assert reply.json()["body"] == "A human reply"

        resumed = await client.post(
            f"/api/v1/staff/support/{handoff_id}/resume-ai",
            json={"version": claimed.json()["version"] + 1},
            headers=headers,
        )
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "AI_ACTIVE"

    answer_service = CountingAnswerService()
    assert await ChatAnswerService(db_session, answer_service).process(stale_job_id) is None
    assert answer_service.calls == 0
    persisted_job = await db_session.get(JobIntent, stale_job_id)
    assert persisted_job is not None
    assert persisted_job.state is JobState.FAILED
    assert persisted_job.last_error_code == "HANDOFF_RESUME_STALE"
    assert (
        list(
            (
                await db_session.scalars(
                    select(ChatMessage).where(
                        ChatMessage.session_id == session_id,
                        ChatMessage.actor.in_([ChatActor.AI, ChatActor.SYSTEM]),
                    )
                )
            ).all()
        )
        == []
    )
    assert (
        list(
            (
                await db_session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == session_id,
                        OutboxEvent.event_type.like("chat.answer.%"),
                    )
                )
            ).all()
        )
        == []
    )
