from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.email.models import EmailSyncState, GmailSyncStatus
from app.modules.email.recovery import GmailRecoveryService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import JobIntent, JobState
from app.modules.outbox.models import OutboxEvent


async def _truncated_state(
    db_session: AsyncSession, email_context: dict[str, object]
) -> tuple[EmailSyncState, Principal]:
    connector = email_context["connector"]
    organization = email_context["organization"]
    staff_user = email_context["staff_user"]
    staff_user.role = UserRole.ADMIN
    state = EmailSyncState(
        organization_id=organization.id,
        connector_id=connector.id,
        mailbox_address="owner@example.test",
        sync_status=GmailSyncStatus.RECOVERY_TRUNCATED,
        recovery_batch_id=uuid4(),
        recovery_baseline_history_id="100",
        recovery_window_started_at=datetime(2026, 10, 1, tzinfo=UTC),
        recovery_page_token="next-page",
        recovery_processed_count=500,
        recovery_message_limit=500,
        recovery_has_more=True,
    )
    db_session.add(state)
    await db_session.flush()
    return state, Principal(
        staff_user.id,
        organization.id,
        staff_user.email,
        UserRole.ADMIN,
        uuid4(),
        "csrf",
    )


@pytest.mark.asyncio
async def test_continue_recovery_extends_bound_and_atomically_schedules_work(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    state, principal = await _truncated_state(db_session, email_context)

    result = await GmailRecoveryService(db_session).continue_recovery(
        principal,
        state.connector_id,
        expected_version=state.version,
        additional_messages=250,
        reason="Continue the documented backlog recovery.",
    )

    job = await db_session.get(JobIntent, result.active_sync_job_id)
    event = await db_session.scalar(
        select(OutboxEvent).where(OutboxEvent.aggregate_id == result.active_sync_job_id)
    )
    assert result.sync_status is GmailSyncStatus.RECOVERY_RUNNING
    assert result.recovery_message_limit == 750
    assert job is not None and job.state is JobState.PENDING
    assert event is not None and event.event_type == "email.gmail_history.requested"


@pytest.mark.asyncio
async def test_accept_limited_recovery_records_choice_and_still_schedules_h0_increment(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    state, principal = await _truncated_state(db_session, email_context)

    result = await GmailRecoveryService(db_session).accept_limited_recovery(
        principal,
        state.connector_id,
        expected_version=state.version,
        confirmed=True,
        reason="The administrator accepts the stated historical coverage gap.",
    )

    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.object_id == state.id,
            AuditEvent.action == "gmail.recovery.accept_limited",
        )
    )
    job = await db_session.get(JobIntent, result.active_sync_job_id)
    assert result.recovery_has_more is False
    assert result.recovery_page_token is None
    assert result.sync_status is GmailSyncStatus.RECOVERY_RUNNING
    assert job is not None
    assert job.payload["history_id"] is None
    assert job.payload["trigger"] == "RECOVERY_ACCEPT_LIMITED"
    assert audit is not None
    assert audit.details["choice"] == "ACCEPT_LIMITED"
    assert audit.details["recovery_processed_count"] == 500
