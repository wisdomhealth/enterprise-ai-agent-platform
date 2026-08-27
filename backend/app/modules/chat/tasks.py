"""Registered Celery consumers for durable customer-chat answer intents."""

import asyncio
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import and_, func, or_, select

from app.core.config import Settings
from app.core.database import async_sessionmaker
from app.modules.chat.answering import CHAT_ANSWER_KIND, ChatAnswerService, CustomerAnswerService
from app.modules.chat.sse import RedisChatEventPublisher
from app.modules.jobs.models import JobIntent, JobState
from app.modules.rag.answer_service import GroundedAnswerService
from app.modules.rag.types import ValidatedAnswer

CHAT_ANSWER_TASK_NAME = "app.modules.chat.tasks.chat_answer"


@shared_task(name=CHAT_ANSWER_TASK_NAME)  # type: ignore[untyped-decorator]
def chat_answer(job_id: str) -> None:
    asyncio.run(_consume_chat_answer(UUID(job_id)))


@shared_task(name="app.modules.chat.tasks.dispatch_pending_chat_answer_jobs")  # type: ignore[untyped-decorator]
def dispatch_pending_chat_answer_jobs() -> None:
    asyncio.run(_dispatch_pending_chat_answer_jobs())


async def _consume_chat_answer(job_id: UUID) -> None:
    settings = Settings()
    async with async_sessionmaker() as db_session:
        try:
            answer_service: CustomerAnswerService = GroundedAnswerService.from_settings(settings)
        except RuntimeError:
            answer_service = _UnavailableAnswerService(settings.grounded_refusal_message)
        publisher = None
        if settings.redis_url is not None:
            from redis.asyncio import Redis

            publisher = RedisChatEventPublisher(
                Redis.from_url(settings.redis_url.unicode_string(), decode_responses=True)
            )
        await ChatAnswerService(
            db_session, answer_service, event_publisher=publisher
        ).process(job_id)


class _UnavailableAnswerService:
    """Safe worker-local fallback when configured providers are unavailable."""

    def __init__(self, refusal_message: str) -> None:
        self._refusal_message = refusal_message

    async def answer(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> ValidatedAnswer:
        return ValidatedAnswer(
            text=self._refusal_message,
            claims=[],
            citations=[],
            segments=[self._refusal_message],
            refused=True,
            model="unavailable",
            prompt_version="unavailable",
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0.0,
        )


async def _dispatch_pending_chat_answer_jobs() -> None:
    """Recover durable pending intents after a broker wake-up failure/restart."""
    async with async_sessionmaker() as db_session:
        job_ids = list(
            (
                await db_session.scalars(
                    select(JobIntent.id).where(
                        JobIntent.kind == CHAT_ANSWER_KIND,
                        or_(
                            JobIntent.state == JobState.PENDING,
                            and_(
                                JobIntent.state == JobState.RUNNING,
                                JobIntent.lease_expires_at.is_not(None),
                                JobIntent.lease_expires_at <= func.clock_timestamp(),
                            ),
                        ),
                    )
                )
            ).all()
        )
    for job_id in job_ids:
        chat_answer.delay(str(job_id))
