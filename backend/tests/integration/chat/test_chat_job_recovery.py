from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery import create_celery
from app.core.config import Settings
from app.main import create_app
from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ChatSessionCredential
from app.modules.chat.rate_limit import SlidingWindowRateLimiter
from app.modules.chat.tokens import ChatTokenService
from app.modules.identity.dependencies import get_db_session
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.models import KnowledgeBase
from app.modules.rag.types import ValidatedAnswer


class ValidAnswerService:
    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        return ValidatedAnswer(
            text="The refund period is 30 days.",
            claims=[],
            citations=[],
            segments=["The refund period is 30 days."],
            refused=False,
            model="fake",
            prompt_version="test",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0.0,
        )


class AlwaysAdmitRedis:
    async def eval(self, _script: str, _numkeys: int, *_args: object) -> list[int]:
        return [1, 0]


async def _session(db_session: AsyncSession) -> ChatSession:
    organization = Organization(name=f"Chat job {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{uuid4().hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
    )
    db_session.add(session)
    await db_session.flush()
    return session


@pytest.mark.asyncio
async def test_duplicate_job_persists_one_ai_message(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    chat_service = ChatAnswerService(db_session, ValidAnswerService())
    _, job = await chat_service.submit_customer_message(session.id, "What is the refund period?")
    await db_session.commit()

    await chat_service.process(job.id)
    await chat_service.process(job.id)

    messages = list(
        (
            await db_session.scalars(
                select(ChatMessage).where(
                    ChatMessage.session_id == session.id,
                    ChatMessage.actor == ChatActor.AI,
                )
            )
        ).all()
    )
    assert len(messages) == 1
    assert messages[0].sequence == 2


@pytest.mark.asyncio
async def test_public_message_write_persists_one_customer_message_and_job(
    db_session: AsyncSession,
) -> None:
    application: FastAPI = create_app(Settings(SESSION_SECRET="task-fourteen-session-secret"))

    async def override_db_session():  # type: ignore[no-untyped-def]
        yield db_session

    application.dependency_overrides[get_db_session] = override_db_session
    application.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    session = await _session(db_session)
    issued = ChatTokenService().issue(session_id=session.id)
    db_session.add(
        ChatSessionCredential(
            session_id=session.id,
            token_hash=issued.token_hash,
            expires_at=issued.expires_at,
        )
    )
    await db_session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://testserver"
    ) as client:
        first = await client.post(
            f"/api/v1/public/chat/sessions/{session.id}/messages",
            json={"body": "Can you help?"},
            headers={
                "Authorization": f"Bearer {issued.value}",
                "Idempotency-Key": "customer-message-1",
            },
        )
        replay = await client.post(
            f"/api/v1/public/chat/sessions/{session.id}/messages",
            json={"body": "Can you help?"},
            headers={
                "Authorization": f"Bearer {issued.value}",
                "Idempotency-Key": "customer-message-1",
            },
        )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert await db_session.scalar(
        select(func.count()).select_from(JobIntent).where(JobIntent.kind == "chat.answer")
    ) == 1


@pytest.mark.asyncio
async def test_registered_celery_consumer_processes_durable_chat_intent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.chat.tasks import CHAT_ANSWER_TASK_NAME, _consume_chat_answer

    session = await _session(db_session)
    chat_service = ChatAnswerService(db_session, ValidAnswerService())
    _, job = await chat_service.submit_customer_message(session.id, "Can you help?")
    await db_session.commit()

    celery = create_celery(Settings())
    celery.loader.import_default_modules()
    monkeypatch.setattr(
        "app.modules.chat.tasks.async_sessionmaker", _SharedSessionFactory(db_session)
    )
    await _consume_chat_answer(job.id)

    assert CHAT_ANSWER_TASK_NAME in celery.tasks
    job_state = await db_session.scalar(select(JobIntent.state).where(JobIntent.id == job.id))
    assert job_state is JobState.SUCCEEDED


@pytest.mark.asyncio
async def test_pending_dispatcher_requeues_expired_running_chat_job(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.chat.tasks import _dispatch_pending_chat_answer_jobs, chat_answer

    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.CUSTOMER,
        body="Can you help?",
    )
    db_session.add(message)
    await db_session.flush()
    job = JobIntent(
        kind="chat.answer",
        idempotency_key=f"expired-chat-{uuid4()}",
        payload={"session_id": str(session.id), "message_id": str(message.id)},
        state=JobState.RUNNING,
        lease_owner="lost-worker",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(job)
    await db_session.commit()
    submitted: list[str] = []
    monkeypatch.setattr(
        "app.modules.chat.tasks.async_sessionmaker", _SharedSessionFactory(db_session)
    )
    monkeypatch.setattr(chat_answer, "delay", lambda job_id: submitted.append(job_id))

    await _dispatch_pending_chat_answer_jobs()

    assert submitted == [str(job.id)]


class _SharedSessionFactory:
    """Use the fixture transaction while exercising the registered task wiring."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> "_SharedSessionFactory":
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None
