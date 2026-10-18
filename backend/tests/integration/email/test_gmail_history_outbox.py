from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.tasks import (
    EMAIL_HISTORY_KIND,
    EMAIL_HISTORY_OUTBOX_EVENT_TYPE,
    _dispatch_email_history_outbox_event,
)
from app.modules.jobs.service import JobService
from app.modules.outbox.models import OutboxEvent
from app.modules.outbox.service import OutboxService


async def _history_event(db_session: AsyncSession) -> tuple[UUID, UUID]:
    job = await JobService().enqueue(
        db_session,
        EMAIL_HISTORY_KIND,
        "history-outbox-fixture",
        {"connector_id": "fixture"},
    )
    event = await OutboxService().add(
        db_session,
        EMAIL_HISTORY_OUTBOX_EVENT_TYPE,
        "job",
        job.id,
        {"job_id": str(job.id)},
    )
    await db_session.commit()
    return job.id, event.event_id


@pytest.mark.asyncio
async def test_history_outbox_marks_published_only_after_broker_acceptance(
    db_session: AsyncSession, monkeypatch
) -> None:
    job_id, event_id = await _history_event(db_session)
    dispatched: list[UUID] = []
    monkeypatch.setattr(
        "app.modules.email.tasks.email_job.delay",
        lambda value: dispatched.append(UUID(value)),
    )

    assert await _dispatch_email_history_outbox_event(event_id, db_session=db_session)

    event = await db_session.get(OutboxEvent, event_id, populate_existing=True)
    assert dispatched == [job_id]
    assert event is not None
    assert event.publish_attempts == 1
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_history_outbox_failure_remains_retryable(
    db_session: AsyncSession, monkeypatch
) -> None:
    _job_id, event_id = await _history_event(db_session)

    def fail(_value: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr("app.modules.email.tasks.email_job.delay", fail)

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await _dispatch_email_history_outbox_event(event_id, db_session=db_session)

    event = await db_session.get(OutboxEvent, event_id, populate_existing=True)
    assert event is not None
    assert event.publish_attempts == 1
    assert event.published_at is None


@pytest.mark.asyncio
async def test_history_outbox_rejects_mismatched_payload(
    db_session: AsyncSession, monkeypatch
) -> None:
    _job_id, event_id = await _history_event(db_session)
    event = await db_session.get(OutboxEvent, event_id)
    assert event is not None
    event.payload = {"job_id": "00000000-0000-0000-0000-000000000000"}
    await db_session.commit()
    monkeypatch.setattr(
        "app.modules.email.tasks.email_job.delay",
        lambda _value: pytest.fail("malformed event must not publish"),
    )

    assert not await _dispatch_email_history_outbox_event(event_id, db_session=db_session)
