"""Registered Celery entry points for durable signed webhook delivery."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import async_sessionmaker
from app.modules.connectors.encryption import EnvelopeCipher, envelope_cipher_from_settings
from app.modules.jobs.models import JobIntent, JobState
from app.modules.outbox.models import OutboxEvent, ProcessedEvent
from app.modules.outbox.service import OutboxService
from app.modules.webhooks.delivery import (
    EVENT_DATA_FIELDS,
    HttpxWebhookTransport,
    WebhookDeliveryService,
    WebhookSubscriptionService,
    validate_webhook_event,
)

WEBHOOK_DELIVERY_TASK_NAME = "app.modules.webhooks.tasks.webhook_delivery_job"
WEBHOOK_SCHEDULER_CONSUMER = "webhook-scheduler-v1"


@shared_task(name=WEBHOOK_DELIVERY_TASK_NAME)  # type: ignore[untyped-decorator]
def webhook_delivery_job(job_id: str) -> None:
    asyncio.run(_consume_webhook_delivery_job(UUID(job_id)))


@shared_task(  # type: ignore[untyped-decorator]
    name="app.modules.webhooks.tasks.dispatch_pending_webhook_events"
)
def dispatch_pending_webhook_events() -> None:
    asyncio.run(_dispatch_pending_webhook_events())


@shared_task(  # type: ignore[untyped-decorator]
    name="app.modules.webhooks.tasks.dispatch_pending_webhook_jobs"
)
def dispatch_pending_webhook_jobs() -> None:
    asyncio.run(_dispatch_pending_webhook_jobs())


async def _consume_webhook_delivery_job(job_id: UUID) -> None:
    settings = Settings()
    async with async_sessionmaker() as db_session:
        await WebhookDeliveryService(
            db_session,
            _required_cipher(settings),
            HttpxWebhookTransport(),
            worker_id=f"celery-webhook:{uuid4()}",
        ).deliver(job_id)


async def _dispatch_pending_webhook_events(*, db_session: AsyncSession | None = None) -> None:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _dispatch_pending_webhook_events(db_session=owned_session)
        return
    settings = Settings()
    handled = select(ProcessedEvent.event_id).where(
        ProcessedEvent.consumer_name == WEBHOOK_SCHEDULER_CONSUMER,
        ProcessedEvent.event_id == OutboxEvent.event_id,
    )
    events = list(
        (
            await db_session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type.in_(tuple(EVENT_DATA_FIELDS)),
                    ~handled.exists(),
                )
                .order_by(OutboxEvent.occurred_at, OutboxEvent.event_id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    job_ids: list[UUID] = []
    service = WebhookSubscriptionService(db_session, envelope_cipher_from_settings(settings))
    for event in events:
        try:
            validate_webhook_event(event)
        except ValueError:
            # Invalid producer data remains unprocessed for durable Outbox recovery.
            continue
        if not await OutboxService().begin_processing(
            db_session,
            WEBHOOK_SCHEDULER_CONSUMER,
            event.event_id,
        ):
            continue
        deliveries = await service.schedule(event)
        job_ids.extend(delivery.job_id for delivery in deliveries)
    await db_session.commit()
    for job_id in job_ids:
        webhook_delivery_job.delay(str(job_id))


async def _dispatch_pending_webhook_jobs() -> None:
    async with async_sessionmaker() as db_session:
        job_ids = list(
            (
                await db_session.scalars(
                    select(JobIntent.id).where(
                        JobIntent.kind == WebhookDeliveryService.JOB_KIND,
                        or_(
                            and_(
                                JobIntent.state == JobState.PENDING,
                                or_(
                                    JobIntent.next_attempt_at.is_(None),
                                    JobIntent.next_attempt_at <= func.clock_timestamp(),
                                ),
                            ),
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
        webhook_delivery_job.delay(str(job_id))


def _required_cipher(settings: Settings) -> EnvelopeCipher:
    cipher = envelope_cipher_from_settings(settings)
    if cipher is not None:
        return cipher
    raise RuntimeError("webhook envelope encryption is not configured")
