from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.modules.outbox.models import OutboxEvent
from app.modules.webhooks.delivery import WebhookDeliveryService


@pytest.mark.asyncio
async def test_redelivery_keeps_event_id_and_increments_attempt() -> None:
    event = OutboxEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        event_type="support.handoff.queued",
        event_version=1,
        aggregate_type="support_handoff",
        aggregate_id=UUID("00000000-0000-0000-0000-000000000002"),
        payload={
            "organization_id": "00000000-0000-0000-0000-000000000003",
            "handoff_id": "00000000-0000-0000-0000-000000000002",
            "session_id": "00000000-0000-0000-0000-000000000004",
            "state": "QUEUED",
            "trigger": "CUSTOMER_REQUEST",
            "last_customer_sequence": 7,
            "raw_customer_body": "must not leave the platform",
        },
        occurred_at=datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
    )

    first = await WebhookDeliveryService.build(
        event,
        attempt=1,
        signing_secret=b"w" * 32,
        timestamp=1_800_000_000,
    )
    second = await WebhookDeliveryService.build(
        event,
        attempt=2,
        signing_secret=b"w" * 32,
        timestamp=1_800_000_010,
    )

    assert first.body == {
        "data": {
            "handoff_id": "00000000-0000-0000-0000-000000000002",
            "organization_id": "00000000-0000-0000-0000-000000000003",
            "session_id": "00000000-0000-0000-0000-000000000004",
            "state": "QUEUED",
            "trigger": "CUSTOMER_REQUEST",
            "last_customer_sequence": 7,
        },
        "delivery_attempt": 1,
        "event_id": "00000000-0000-0000-0000-000000000001",
        "event_type": "support.handoff.queued",
        "event_version": 1,
        "occurred_at": "2026-09-03T12:30:00Z",
    }
    assert second.body["event_id"] == first.body["event_id"]
    assert second.body["delivery_attempt"] == 2
    assert b"raw_customer_body" not in first.body_bytes
    assert first.headers["X-Webhook-Timestamp"] == "1800000000"
    assert first.headers["X-Webhook-Signature"] != second.headers["X-Webhook-Signature"]


@pytest.mark.asyncio
async def test_non_allowlisted_event_fails_closed() -> None:
    event = OutboxEvent(
        event_type="chat.answer.validated",
        event_version=1,
        aggregate_type="chat_message",
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        payload={"body": "private answer"},
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="not allowed"):
        await WebhookDeliveryService.build(
            event,
            attempt=1,
            signing_secret=b"w" * 32,
            timestamp=1_800_000_000,
        )


@pytest.mark.asyncio
async def test_unknown_event_version_fails_closed_before_signing() -> None:
    event = OutboxEvent(
        event_type="support.handoff.queued",
        event_version=999,
        aggregate_type="support_handoff",
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        payload={
            "organization_id": "00000000-0000-0000-0000-000000000002",
            "handoff_id": "00000000-0000-0000-0000-000000000001",
            "trigger": "CUSTOMER_REQUEST",
            "last_customer_sequence": 1,
        },
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="schema"):
        await WebhookDeliveryService.build(
            event,
            attempt=1,
            signing_secret=b"w" * 32,
            timestamp=1_800_000_000,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_update",
    [
        {"organization_id": "not-a-uuid"},
        {"trigger": "NOT_A_TRIGGER"},
        {"trigger": None},
        {"last_customer_sequence": True},
        {"last_customer_sequence": -1},
    ],
)
async def test_allowlisted_schema_rejects_malformed_typed_data(
    payload_update: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "organization_id": "00000000-0000-0000-0000-000000000002",
        "handoff_id": "00000000-0000-0000-0000-000000000001",
        "trigger": "CUSTOMER_REQUEST",
        "last_customer_sequence": 1,
    }
    payload.update(payload_update)
    event = OutboxEvent(
        event_type="support.handoff.queued",
        event_version=1,
        aggregate_type="support_handoff",
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        payload=payload,
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="invalid data"):
        await WebhookDeliveryService.build(
            event,
            attempt=1,
            signing_secret=b"w" * 32,
            timestamp=1_800_000_000,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "support.handoff.replied",
            {
                "organization_id": "00000000-0000-0000-0000-000000000002",
                "message_id": "00000000-0000-0000-0000-000000000003",
                "sequence": 2,
            },
        ),
        (
            "support.handoff.ai_resumed",
            {
                "organization_id": "00000000-0000-0000-0000-000000000002",
                "session_id": "00000000-0000-0000-0000-000000000003",
                "handoff_boundary": 2,
                "await_customer_message": True,
            },
        ),
    ],
)
async def test_published_schema_accepts_valid_handoff_sequence_values(
    event_type: str,
    payload: dict[str, object],
) -> None:
    event = OutboxEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000004"),
        event_type=event_type,
        event_version=1,
        aggregate_type="support_handoff",
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        payload=payload,
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
    )

    request = await WebhookDeliveryService.build(
        event,
        attempt=1,
        signing_secret=b"w" * 32,
        timestamp=1_800_000_000,
    )

    assert request.body["data"] == payload


@pytest.mark.asyncio
async def test_allowlisted_event_with_malformed_data_fails_closed() -> None:
    event = OutboxEvent(
        event_type="support.handoff.queued",
        event_version=1,
        aggregate_type="support_handoff",
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        payload={
            "organization_id": "00000000-0000-0000-0000-000000000002",
            "handoff_id": "00000000-0000-0000-0000-000000000001",
            "trigger": {"customer_body": "must not escape"},
            "last_customer_sequence": 1,
        },
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="invalid data"):
        await WebhookDeliveryService.build(
            event,
            attempt=1,
            signing_secret=b"w" * 32,
            timestamp=1_800_000_000,
        )
