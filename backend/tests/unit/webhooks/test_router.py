from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app


@pytest.mark.asyncio
async def test_subscription_api_requires_staff_authentication() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings())),
        base_url="https://testserver",
    ) as client:
        listed = await client.get("/api/v1/admin/webhooks/subscriptions")
        created = await client.post(
            "/api/v1/admin/webhooks/subscriptions",
            headers={"Idempotency-Key": "unauthorized-create"},
            json={
                "endpoint_url": "https://hooks.example.test/api",
                "event_types": ["support.handoff.queued"],
                "signing_secret": "unauthorized-test-secret-with-at-least-32-bytes",
            },
        )

    assert listed.status_code == 401
    assert created.status_code == 401
