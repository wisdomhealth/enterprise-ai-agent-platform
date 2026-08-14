from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.modules.idempotency.models import IdempotencyState
from app.modules.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyService,
)


@pytest.fixture
def idempotency_service(db_session) -> IdempotencyService:
    return IdempotencyService(db_session, lease_seconds=60)


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_rebound(idempotency_service):
    first = await idempotency_service.begin(
        scope_id=uuid4(),
        actor_id=uuid4(),
        operation="support.claim",
        object_id=uuid4(),
        key="request-key-1",
        request_hash="hash-a",
    )
    with pytest.raises(IdempotencyConflict):
        await idempotency_service.begin(
            scope_id=first.scope_id,
            actor_id=first.actor_id,
            operation="support.resolve",
            object_id=first.object_id,
            key="request-key-1",
            request_hash="hash-b",
        )


@pytest.mark.asyncio
async def test_completed_matching_request_replays_safe_response(idempotency_service):
    record = await idempotency_service.begin(
        scope_id=uuid4(),
        actor_id=uuid4(),
        operation="support.claim",
        object_id=uuid4(),
        key="request-key-replay",
        request_hash="hash-a",
    )
    await idempotency_service.complete(
        record.id,
        status_code=200,
        response_body={"ticket_id": str(record.object_id), "secret": "must-not-persist"},
        safe_response_keys={"ticket_id"},
    )

    replay = await idempotency_service.begin(
        scope_id=record.scope_id,
        actor_id=record.actor_id,
        operation=record.operation,
        object_id=record.object_id,
        key=record.key,
        request_hash=record.request_hash,
    )

    assert replay.state is IdempotencyState.COMPLETED
    assert replay.status_code == 200
    assert replay.response_body == {"ticket_id": str(record.object_id)}


@pytest.mark.asyncio
async def test_matching_live_request_reports_in_progress(idempotency_service):
    record = await idempotency_service.begin(
        scope_id=uuid4(),
        actor_id=uuid4(),
        operation="support.claim",
        object_id=uuid4(),
        key="request-key-live",
        request_hash="hash-a",
    )

    with pytest.raises(IdempotencyInProgress):
        await idempotency_service.begin(
            scope_id=record.scope_id,
            actor_id=record.actor_id,
            operation=record.operation,
            object_id=record.object_id,
            key=record.key,
            request_hash=record.request_hash,
        )


@pytest.mark.asyncio
async def test_expired_matching_request_lease_can_be_recovered(
    idempotency_service, db_session
):
    record = await idempotency_service.begin(
        scope_id=uuid4(),
        actor_id=uuid4(),
        operation="support.claim",
        object_id=uuid4(),
        key="request-key-expired",
        request_hash="hash-a",
    )
    await db_session.execute(
        update(type(record))
        .where(type(record).id == record.id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    recovered = await idempotency_service.begin(
        scope_id=record.scope_id,
        actor_id=record.actor_id,
        operation=record.operation,
        object_id=record.object_id,
        key=record.key,
        request_hash=record.request_hash,
    )

    assert recovered.id == record.id
    assert recovered.state is IdempotencyState.IN_PROGRESS
    assert recovered.lease_expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_same_binding_with_different_request_hash_conflicts(idempotency_service):
    record = await idempotency_service.begin(
        scope_id=uuid4(),
        actor_id=uuid4(),
        operation="support.claim",
        object_id=uuid4(),
        key="request-key-hash",
        request_hash="hash-a",
    )

    with pytest.raises(IdempotencyConflict):
        await idempotency_service.begin(
            scope_id=record.scope_id,
            actor_id=record.actor_id,
            operation=record.operation,
            object_id=record.object_id,
            key=record.key,
            request_hash="hash-b",
        )
