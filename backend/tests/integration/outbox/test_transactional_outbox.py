from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.modules.outbox.dispatcher import OutboxDispatcher
from app.modules.outbox.models import OutboxEvent, ProcessedEvent
from app.modules.outbox.service import OutboxService


@pytest.fixture
def outbox_service() -> OutboxService:
    return OutboxService()


async def count_outbox_events(db_session) -> int:
    count = await db_session.scalar(select(func.count()).select_from(OutboxEvent))
    return int(count or 0)


@pytest.mark.asyncio
async def test_business_rollback_also_rolls_back_outbox(db_session, outbox_service):
    aggregate_id = uuid4()
    await outbox_service.add(
        db_session,
        "resource.created",
        "resource",
        aggregate_id,
        {"resource_id": str(aggregate_id)},
    )
    await db_session.rollback()

    assert await count_outbox_events(db_session) == 0


@pytest.mark.asyncio
async def test_failed_publish_remains_pending_and_increments_attempts(
    db_session, outbox_service
):
    event = await outbox_service.add(
        db_session,
        "resource.created",
        "resource",
        uuid4(),
        {"resource_id": str(uuid4())},
    )
    await db_session.flush()

    async def fail_publish(_event: OutboxEvent) -> None:
        raise RuntimeError("broker unavailable")

    published_count = await OutboxDispatcher(fail_publish).dispatch_pending(db_session)
    await db_session.refresh(event)

    assert published_count == 0
    assert event.published_at is None
    assert event.publish_attempts == 1


@pytest.mark.asyncio
async def test_successful_publish_marks_event_after_delivery(db_session, outbox_service):
    event = await outbox_service.add(
        db_session,
        "resource.created",
        "resource",
        uuid4(),
        {"resource_id": str(uuid4())},
    )
    delivered_event_ids = []

    async def publish(outbox_event: OutboxEvent) -> None:
        delivered_event_ids.append(outbox_event.event_id)

    published_count = await OutboxDispatcher(publish).dispatch_pending(db_session)
    await db_session.refresh(event)

    assert published_count == 1
    assert delivered_event_ids == [event.event_id]
    assert event.published_at is not None
    assert event.publish_attempts == 1


@pytest.mark.asyncio
async def test_consumer_processes_each_event_only_once(db_session, outbox_service):
    event_id = uuid4()

    first = await outbox_service.begin_processing(db_session, "search-indexer", event_id)
    second = await outbox_service.begin_processing(db_session, "search-indexer", event_id)

    assert first is True
    assert second is False
    processed_count = await db_session.scalar(
        select(func.count())
        .select_from(ProcessedEvent)
        .where(
            ProcessedEvent.consumer_name == "search-indexer",
            ProcessedEvent.event_id == event_id,
        )
    )
    assert processed_count == 1
