from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from app.core.database import async_sessionmaker, engine
from app.modules.connectors.encryption import EnvelopeCipher, FileKeyWrapper
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent, JobState
from app.modules.outbox.models import OutboxEvent
from app.modules.webhooks.delivery import (
    WebhookDeliveryService,
    WebhookSubscriptionService,
    WebhookTransportResponse,
)
from app.modules.webhooks.models import (
    WebhookDelivery,
    WebhookDeliveryState,
    WebhookSubscription,
)
from app.modules.webhooks.signing import WebhookSigner


class RecordingTransport:
    def __init__(self, responses: list[WebhookTransportResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> WebhookTransportResponse:
        self.calls.append({"url": url, "body": body, "headers": headers})
        return self.responses.pop(0)


@dataclass(frozen=True, slots=True)
class CommittedWebhook:
    organization_id: UUID
    subscription_id: UUID
    event_id: UUID
    job_id: UUID | None
    cipher: EnvelopeCipher


async def _seed_committed_webhook(tmp_path: Path, *, schedule: bool) -> CommittedWebhook:
    key_path = tmp_path / f"webhook-{uuid4()}.key"
    key_path.write_bytes(b"k" * 32)
    cipher = EnvelopeCipher(FileKeyWrapper(key_path))
    async with async_sessionmaker() as session:
        organization = Organization(name=f"Webhook recovery {uuid4()}")
        session.add(organization)
        await session.flush()
        admin = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"webhook-recovery-{uuid4()}",
            email="webhook-recovery@example.test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(admin)
        await session.flush()
        principal = Principal(
            admin.id,
            organization.id,
            admin.email,
            admin.role,
            uuid4(),
            "webhook-recovery-csrf",
        )
        service = WebhookSubscriptionService(session, cipher)
        subscription = await service.create(
            principal,
            endpoint_url="https://hooks.example.test/cross-session",
            event_types=["support.handoff.queued"],
            signing_secret="cross-session-signing-secret-at-least-32-bytes",
        )
        event = OutboxEvent(
            event_type="support.handoff.queued",
            event_version=1,
            aggregate_type="support_handoff",
            aggregate_id=subscription.id,
            payload={
                "organization_id": str(organization.id),
                "handoff_id": str(subscription.id),
                "trigger": "CUSTOMER_REQUEST",
                "last_customer_sequence": 1,
            },
            occurred_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
        deliveries = await service.schedule(event) if schedule else []
        await session.commit()
        return CommittedWebhook(
            organization_id=organization.id,
            subscription_id=subscription.id,
            event_id=event.event_id,
            job_id=deliveries[0].job_id if deliveries else None,
            cipher=cipher,
        )


async def _cleanup_committed_webhook(context: CommittedWebhook) -> None:
    async with async_sessionmaker() as session:
        job_ids = list(
            (
                await session.scalars(
                    select(WebhookDelivery.job_id).where(
                        WebhookDelivery.organization_id == context.organization_id
                    )
                )
            ).all()
        )
        await session.execute(
            delete(WebhookDelivery).where(
                WebhookDelivery.organization_id == context.organization_id
            )
        )
        await session.execute(
            delete(WebhookSubscription).where(
                WebhookSubscription.organization_id == context.organization_id
            )
        )
        if job_ids:
            await session.execute(delete(JobIntent).where(JobIntent.id.in_(job_ids)))
        await session.execute(
            delete(OutboxEvent).where(OutboxEvent.aggregate_id == context.subscription_id)
        )
        await session.execute(
            delete(Organization).where(Organization.id == context.organization_id)
        )
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_uses_durable_job_and_same_event_without_duplicate_delivery(
    db_session,
    webhook_context,
) -> None:  # type: ignore[no-untyped-def]
    subscription_service = WebhookSubscriptionService(db_session, webhook_context["cipher"])
    principal = webhook_context["principal"](webhook_context["admin"])
    signing_secret = "delivery-test-secret-with-at-least-32-bytes"
    subscription = await subscription_service.create(
        principal,
        endpoint_url="https://hooks.example.test/recovery",
        event_types=["support.handoff.queued"],
        signing_secret=signing_secret,
    )
    event = OutboxEvent(
        event_type="support.handoff.queued",
        event_version=1,
        aggregate_type="support_handoff",
        aggregate_id=subscription.id,
        payload={
            "organization_id": str(principal.organization_id),
            "handoff_id": str(subscription.id),
            "trigger": "CUSTOMER_REQUEST",
            "last_customer_sequence": 1,
        },
        occurred_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.flush()
    delivery = (await subscription_service.schedule(event))[0]
    job_id = delivery.job_id
    await db_session.commit()
    transport = RecordingTransport(
        [
            WebhookTransportResponse(503, b"authorization=must-not-persist"),
            WebhookTransportResponse(204, b"accepted"),
        ]
    )

    first = await WebhookDeliveryService(
        db_session,
        webhook_context["cipher"],
        transport,
        worker_id="webhook-worker-a",
    ).deliver(job_id)

    assert first is not None
    assert first.state is WebhookDeliveryState.RETRY_WAIT
    assert first.delivery_attempt == 1
    assert first.response_summary is not None
    assert "must-not-persist" not in first.response_summary
    first_job = await db_session.get(JobIntent, job_id)
    assert first_job is not None
    assert first_job.state is JobState.PENDING
    first_job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    second = await WebhookDeliveryService(
        db_session,
        webhook_context["cipher"],
        transport,
        worker_id="webhook-worker-b",
    ).deliver(job_id)

    assert second is not None
    assert second.state is WebhookDeliveryState.SUCCEEDED
    assert second.delivery_attempt == 2
    assert len(transport.calls) == 2
    first_body = json.loads(transport.calls[0]["body"])
    second_body = json.loads(transport.calls[1]["body"])
    assert first_body["event_id"] == second_body["event_id"] == str(event.event_id)
    assert [first_body["delivery_attempt"], second_body["delivery_attempt"]] == [1, 2]
    for call in transport.calls:
        timestamp = int(call["headers"]["X-Webhook-Timestamp"])
        assert WebhookSigner(signing_secret.encode()).verify(
            body=call["body"],
            timestamp=timestamp,
            signature=call["headers"]["X-Webhook-Signature"],
            now=timestamp,
        )

    assert (
        await WebhookDeliveryService(
            db_session,
            webhook_context["cipher"],
            transport,
            worker_id="webhook-worker-c",
        ).deliver(job_id)
        is None
    )
    assert len(transport.calls) == 2
    assert (
        len(
            list(
                (
                    await db_session.scalars(
                        select(WebhookDelivery).where(WebhookDelivery.event_id == event.event_id)
                    )
                ).all()
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_delivery_retry_and_success_recover_across_fresh_sessions(tmp_path: Path) -> None:
    context = await _seed_committed_webhook(tmp_path, schedule=True)
    assert context.job_id is not None
    transport = RecordingTransport(
        [
            WebhookTransportResponse(503, b"authorization=must-not-persist"),
            WebhookTransportResponse(204, b"accepted"),
        ]
    )
    try:
        async with async_sessionmaker() as first_worker:
            failed = await WebhookDeliveryService(
                first_worker,
                context.cipher,
                transport,
                worker_id="webhook-cross-session-a",
            ).deliver(context.job_id)
            assert failed is not None
            assert failed.state is WebhookDeliveryState.RETRY_WAIT

        async with async_sessionmaker() as recovery:
            durable_delivery = await recovery.scalar(
                select(WebhookDelivery).where(WebhookDelivery.job_id == context.job_id)
            )
            durable_job = await recovery.get(JobIntent, context.job_id)
            assert durable_delivery is not None
            assert durable_delivery.state is WebhookDeliveryState.RETRY_WAIT
            assert durable_delivery.response_summary is not None
            assert "must-not-persist" not in durable_delivery.response_summary
            assert durable_job is not None and durable_job.state is JobState.PENDING
            durable_job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await recovery.commit()

        async with async_sessionmaker() as second_worker:
            succeeded = await WebhookDeliveryService(
                second_worker,
                context.cipher,
                transport,
                worker_id="webhook-cross-session-b",
            ).deliver(context.job_id)
            assert succeeded is not None
            assert succeeded.state is WebhookDeliveryState.SUCCEEDED

        async with async_sessionmaker() as verify:
            durable_delivery = await verify.scalar(
                select(WebhookDelivery).where(WebhookDelivery.job_id == context.job_id)
            )
            durable_job = await verify.get(JobIntent, context.job_id)
            assert durable_delivery is not None
            assert durable_delivery.state is WebhookDeliveryState.SUCCEEDED
            assert durable_delivery.delivery_attempt == 2
            assert durable_job is not None and durable_job.state is JobState.SUCCEEDED
            assert (
                await verify.scalar(
                    select(func.count(WebhookDelivery.id)).where(
                        WebhookDelivery.event_id == context.event_id
                    )
                )
                == 1
            )

        async with async_sessionmaker() as duplicate_worker:
            assert (
                await WebhookDeliveryService(
                    duplicate_worker,
                    context.cipher,
                    transport,
                    worker_id="webhook-cross-session-c",
                ).deliver(context.job_id)
                is None
            )
        assert len(transport.calls) == 2
    finally:
        await _cleanup_committed_webhook(context)


@pytest.mark.asyncio
async def test_concurrent_scheduling_persists_one_delivery_and_job(tmp_path: Path) -> None:
    context = await _seed_committed_webhook(tmp_path, schedule=False)

    async def schedule_once() -> tuple[UUID, UUID]:
        async with async_sessionmaker() as session:
            event = await session.get(OutboxEvent, context.event_id)
            assert event is not None
            delivery = (await WebhookSubscriptionService(session, context.cipher).schedule(event))[
                0
            ]
            await session.commit()
            return delivery.id, delivery.job_id

    try:
        first, second = await asyncio.gather(schedule_once(), schedule_once())
        assert first == second
        async with async_sessionmaker() as verify:
            assert (
                await verify.scalar(
                    select(func.count(WebhookDelivery.id)).where(
                        WebhookDelivery.event_id == context.event_id
                    )
                )
                == 1
            )
            assert (
                await verify.scalar(
                    select(func.count(JobIntent.id)).where(
                        JobIntent.kind == WebhookDeliveryService.JOB_KIND,
                        JobIntent.idempotency_key
                        == f"webhook:{context.subscription_id}:{context.event_id}",
                    )
                )
                == 1
            )
    finally:
        await _cleanup_committed_webhook(context)
