from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.main import create_app
from app.modules.identity.dependencies import (
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.jobs.models import JobIntent
from app.modules.outbox.models import OutboxEvent
from app.modules.webhooks.delivery import WebhookSubscriptionService
from app.modules.webhooks.models import WebhookDelivery, WebhookSubscription


@pytest.mark.asyncio
async def test_subscription_secret_is_encrypted_and_event_intent_is_idempotent(
    db_session,
    webhook_context,
) -> None:  # type: ignore[no-untyped-def]
    service = WebhookSubscriptionService(db_session, webhook_context["cipher"])
    admin = webhook_context["principal"](webhook_context["admin"])
    secret = "test-webhook-secret-with-at-least-32-bytes"
    subscription = await service.create(
        admin,
        endpoint_url="https://hooks.example.test/n8n",
        event_types=["support.handoff.queued"],
        signing_secret=secret,
    )
    event = OutboxEvent(
        event_type="support.handoff.queued",
        event_version=1,
        aggregate_type="support_handoff",
        aggregate_id=subscription.id,
        payload={
            "organization_id": str(admin.organization_id),
            "handoff_id": str(subscription.id),
            "trigger": "CUSTOMER_REQUEST",
        },
        occurred_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.flush()

    first = await service.schedule(event)
    second = await service.schedule(event)
    await db_session.commit()

    assert len(first) == 1
    assert [delivery.id for delivery in second] == [first[0].id]
    stored = await db_session.get(WebhookSubscription, subscription.id)
    assert stored is not None
    assert secret.encode() not in stored.secret_ciphertext
    assert await service.load_signing_secret(stored) == secret
    deliveries = list(
        (
            await db_session.scalars(
                select(WebhookDelivery).where(WebhookDelivery.event_id == event.event_id)
            )
        ).all()
    )
    assert len(deliveries) == 1
    assert (
        await db_session.scalar(
            select(func.count(JobIntent.id)).where(JobIntent.id == deliveries[0].job_id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_non_admin_and_foreign_admin_cannot_manage_subscription(
    db_session,
    webhook_context,
) -> None:  # type: ignore[no-untyped-def]
    service = WebhookSubscriptionService(db_session, webhook_context["cipher"])
    member = webhook_context["principal"](webhook_context["member"])
    foreign = webhook_context["principal"](webhook_context["foreign_admin"])

    with pytest.raises(LookupError):
        await service.create(
            member,
            endpoint_url="https://hooks.example.test/member",
            event_types=["support.handoff.queued"],
            signing_secret="member-test-secret-with-at-least-32-bytes",
        )

    admin = webhook_context["principal"](webhook_context["admin"])
    subscription = await service.create(
        admin,
        endpoint_url="https://hooks.example.test/admin",
        event_types=["support.handoff.queued"],
        signing_secret="admin-test-secret-with-at-least-32-bytes",
    )
    assert await service.list_authorized(foreign) == []
    with pytest.raises(LookupError):
        await service.disable(foreign, subscription.id, expected_version=1)


@pytest.mark.asyncio
async def test_admin_api_is_idempotent_and_never_returns_the_signing_secret(
    db_session,
    webhook_context,
) -> None:  # type: ignore[no-untyped-def]
    app = create_app(Settings.model_validate({"SESSION_SECRET": "webhook-test-session"}))
    principal = webhook_context["principal"](webhook_context["admin"])

    async def override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def override_principal():  # type: ignore[no-untyped-def]
        return principal

    app.state.webhook_cipher = webhook_context["cipher"]
    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_staff_session] = override_principal
    app.dependency_overrides[require_staff_csrf] = override_principal
    secret = "api-test-signing-secret-with-at-least-32-bytes"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"X-CSRF-Token": "csrf"},
    ) as client:
        first = await client.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers={"Idempotency-Key": "create-webhook"},
            json={
                "endpoint_url": "https://hooks.example.test/api",
                "event_types": ["support.handoff.queued"],
                "signing_secret": secret,
            },
        )
        replay = await client.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers={"Idempotency-Key": "create-webhook"},
            json={
                "endpoint_url": "https://hooks.example.test/api",
                "event_types": ["support.handoff.queued"],
                "signing_secret": secret,
            },
        )
        listed = await client.get("/api/v1/admin/webhooks/subscriptions")

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert listed.status_code == 200
    assert listed.json() == [first.json()]
    assert secret not in f"{first.text}{replay.text}{listed.text}"
