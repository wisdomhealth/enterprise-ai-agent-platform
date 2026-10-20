from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.models import EmailSyncState, GmailPushReceipt, GmailSyncStatus
from app.modules.email.push import GmailPushService, VerifiedGmailNotification
from app.modules.email.tasks import _consume_email_job, _dispatch_email_history_outbox_event
from app.modules.jobs.models import JobIntent, JobState
from app.modules.outbox.models import OutboxEvent


def _notification(message_id: str, history_id: str) -> VerifiedGmailNotification:
    return VerifiedGmailNotification(
        subscription_name="projects/p/subscriptions/gmail",
        pubsub_message_id=message_id,
        published_at=datetime.now(UTC),
        mailbox_address="staff@example.test",
        history_id=history_id,
    )


async def _state(
    db_session: AsyncSession,
    email_context: dict[str, object],
    *,
    enabled: bool = True,
) -> EmailSyncState:
    connector = email_context["connector"]
    state = EmailSyncState(
        organization_id=connector.organization_id,
        connector_id=connector.id,
        mailbox_address="staff@example.test",
        processed_history_id="100",
        auto_sync_enabled=enabled,
    )
    db_session.add(state)
    await db_session.commit()
    return state


@pytest.mark.asyncio
async def test_push_atomically_persists_receipt_high_water_job_and_outbox(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    state = await _state(db_session, email_context)
    service = GmailPushService(db_session, receipt_retention_days=14)

    job_id = await service.accept(_notification("push-1", "105"))
    duplicate = await service.accept(_notification("push-1", "105"))

    await db_session.refresh(state)
    assert job_id is not None
    assert duplicate is None
    assert state.pending_history_id == "105"
    assert state.active_sync_job_id == job_id
    assert state.last_success_at is None
    assert await db_session.scalar(
        select(func.count(GmailPushReceipt.id)).where(
            GmailPushReceipt.connector_id == state.connector_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.payload["connector_id"].astext == str(state.connector_id)
        )
    ) == 1
    event = await db_session.scalar(
        select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id)
    )
    assert event is not None
    assert event.payload["job_id"] == str(job_id)
    assert event.published_at is None


@pytest.mark.asyncio
async def test_push_outbox_and_worker_form_one_durable_ingestion_chain(
    db_session: AsyncSession,
    email_context: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = await _state(db_session, email_context)
    job_id = await GmailPushService(db_session, receipt_retention_days=14).accept(
        _notification("push-chain", "105")
    )
    assert job_id is not None
    event = await db_session.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))
    assert event is not None

    published_jobs: list[str] = []
    monkeypatch.setattr(
        "app.modules.email.tasks.email_job.delay", published_jobs.append
    )
    assert await _dispatch_email_history_outbox_event(event.event_id, db_session=db_session)
    assert published_jobs == [str(job_id)]

    async def consume_history(db, claimed_job_id, payload, settings, owner, version):
        del claimed_job_id, payload, settings, owner, version
        locked_state = await db.scalar(
            select(EmailSyncState).where(EmailSyncState.connector_id == state.connector_id)
        )
        assert locked_state is not None
        locked_state.processed_history_id = "105"
        return False

    monkeypatch.setattr(
        "app.modules.email.tasks._consume_history_with_lease_renewal", consume_history
    )

    class SharedSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    monkeypatch.setattr("app.modules.email.tasks.async_sessionmaker", SharedSession)
    await _consume_email_job(job_id)

    await db_session.rollback()
    await db_session.refresh(state)
    job = await db_session.get(JobIntent, job_id, populate_existing=True)
    assert job is not None
    assert job.state is JobState.SUCCEEDED
    assert state.processed_history_id == "105"
    assert state.pending_history_id is None
    assert state.active_sync_job_id is None
    assert state.sync_status is GmailSyncStatus.IDLE
    assert state.last_success_at is not None


@pytest.mark.asyncio
async def test_old_notification_is_receipted_without_creating_empty_work(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    state = await _state(db_session, email_context)

    result = await GmailPushService(db_session, receipt_retention_days=14).accept(
        _notification("push-old", "99")
    )

    await db_session.refresh(state)
    assert result is None
    assert state.pending_history_id is None
    assert state.active_sync_job_id is None
    assert await db_session.scalar(
        select(func.count(GmailPushReceipt.id)).where(
            GmailPushReceipt.connector_id == state.connector_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.payload["connector_id"].astext == str(state.connector_id)
        )
    ) == 0


@pytest.mark.asyncio
async def test_disabled_automatic_sync_retains_receipt_without_work(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    state = await _state(db_session, email_context, enabled=False)

    result = await GmailPushService(db_session, receipt_retention_days=14).accept(
        _notification("push-stopped", "109")
    )

    await db_session.refresh(state)
    assert result is None
    assert state.pending_history_id is None
    assert state.active_sync_job_id is None
    assert await db_session.scalar(
        select(func.count(GmailPushReceipt.id)).where(
            GmailPushReceipt.connector_id == state.connector_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.payload["connector_id"].astext == str(state.connector_id)
        )
    ) == 0
