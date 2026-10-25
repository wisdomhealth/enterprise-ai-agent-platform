from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.email.models import EmailSyncState, GmailSyncStatus
from app.modules.email.push import GmailPushService, VerifiedGmailNotification
from app.modules.email.recovery import GmailRecoveryService
from app.modules.email.tasks import _enqueue_gmail_history_jobs
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


@pytest.mark.asyncio
async def test_manual_sync_returns_existing_active_job(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    organization = email_context["organization"]
    staff_user = email_context["staff_user"]
    staff_user.role = UserRole.ADMIN
    state = EmailSyncState(
        organization_id=organization.id,
        connector_id=connector.id,
        processed_history_id="100",
    )
    db_session.add(state)
    await db_session.flush()
    principal = Principal(
        staff_user.id,
        organization.id,
        staff_user.email,
        UserRole.ADMIN,
        uuid4(),
        "csrf",
    )
    service = GmailRecoveryService(db_session)

    first = await service.manual_sync(
        principal,
        connector.id,
        expected_version=state.version,
        reason="Requested from the staff email queue.",
    )
    first_job_id = first.active_sync_job_id
    second = await service.manual_sync(
        principal,
        connector.id,
        expected_version=first.version,
        reason="Repeated click from the staff email queue.",
    )

    assert first_job_id is not None
    assert second.active_sync_job_id == first_job_id
    assert len(
        list(
            (
                await db_session.scalars(
                    select(JobIntent).where(
                        JobIntent.kind == "email.gmail_history",
                        JobIntent.payload["connector_id"].astext == str(connector.id),
                    )
                )
            ).all()
        )
    ) == 1


@pytest.mark.asyncio
async def test_manual_sync_replaces_terminal_active_job_reference(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    organization = email_context["organization"]
    staff_user = email_context["staff_user"]
    staff_user.role = UserRole.ADMIN
    failed_job = JobIntent(
        kind="email.gmail_history",
        idempotency_key=f"email.gmail_history:{connector.id}:failed",
        payload={"connector_id": str(connector.id)},
        state=JobState.FAILED,
    )
    db_session.add(failed_job)
    await db_session.flush()
    state = EmailSyncState(
        organization_id=organization.id,
        connector_id=connector.id,
        processed_history_id="100",
        active_sync_job_id=failed_job.id,
    )
    db_session.add(state)
    await db_session.flush()
    principal = Principal(
        staff_user.id,
        organization.id,
        staff_user.email,
        UserRole.ADMIN,
        uuid4(),
        "csrf",
    )

    result = await GmailRecoveryService(db_session).manual_sync(
        principal,
        connector.id,
        expected_version=state.version,
        reason="Replace stale terminal job reference.",
    )

    assert result.active_sync_job_id is not None
    assert result.active_sync_job_id != failed_job.id
    replacement = await db_session.get(JobIntent, result.active_sync_job_id)
    assert replacement is not None
    assert replacement.state is JobState.PENDING


@pytest.mark.asyncio
async def test_manual_scheduled_and_push_share_one_effective_job(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    organization = email_context["organization"]
    staff_user = email_context["staff_user"]
    staff_user.role = UserRole.ADMIN
    state = EmailSyncState(
        organization_id=organization.id,
        connector_id=connector.id,
        mailbox_address="owner@example.test",
        processed_history_id="100",
        auto_sync_enabled=True,
    )
    db_session.add(state)
    await db_session.flush()
    principal = Principal(
        staff_user.id,
        organization.id,
        staff_user.email,
        UserRole.ADMIN,
        uuid4(),
        "csrf",
    )

    manual = await GmailRecoveryService(db_session).manual_sync(
        principal,
        connector.id,
        expected_version=state.version,
        reason="Coordinate all triggers.",
    )
    await _enqueue_gmail_history_jobs(
        db_session=db_session,
        now=datetime(2026, 10, 21, tzinfo=UTC),
        interval_seconds=3600,
    )
    push_job_id = await GmailPushService(db_session, receipt_retention_days=14).accept(
        VerifiedGmailNotification(
            subscription_name="projects/p/subscriptions/gmail",
            pubsub_message_id="shared-trigger",
            published_at=datetime(2026, 10, 21, tzinfo=UTC),
            mailbox_address="owner@example.test",
            history_id="105",
        )
    )

    assert push_job_id == manual.active_sync_job_id
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "email.gmail_history",
            JobIntent.payload["connector_id"].astext == str(connector.id),
        )
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sync_status",
    [GmailSyncStatus.PAUSED_ERROR, GmailSyncStatus.RECOVERY_REQUIRED],
)
async def test_manual_sync_cannot_bypass_recovery_authorization(
    db_session: AsyncSession,
    email_context: dict[str, object],
    sync_status: GmailSyncStatus,
) -> None:
    connector = email_context["connector"]
    organization = email_context["organization"]
    staff_user = email_context["staff_user"]
    staff_user.role = UserRole.ADMIN
    state = EmailSyncState(
        organization_id=organization.id,
        connector_id=connector.id,
        processed_history_id="100",
        sync_status=sync_status,
    )
    db_session.add(state)
    await db_session.flush()
    principal = Principal(
        staff_user.id,
        organization.id,
        staff_user.email,
        UserRole.ADMIN,
        uuid4(),
        "csrf",
    )

    with pytest.raises(ValueError, match="recovery"):
        await GmailRecoveryService(db_session).manual_sync(
            principal,
            connector.id,
            expected_version=state.version,
            reason="Do not bypass recovery.",
        )


@pytest.mark.asyncio
async def test_resume_after_reauthorization_preserves_cursor_and_downstream_jobs(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    organization = email_context["organization"]
    staff_user = email_context["staff_user"]
    staff_user.role = UserRole.ADMIN
    state = EmailSyncState(
        organization_id=organization.id,
        connector_id=connector.id,
        processed_history_id="cursor-100",
        pending_history_id="cursor-105",
        sync_status=GmailSyncStatus.PAUSED_ERROR,
        last_error_code="GMAIL_REAUTH_REQUIRED",
    )
    db_session.add(state)
    await db_session.flush()
    downstream_before = await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind.in_(("email.classify", "email.draft"))
        )
    )
    principal = Principal(
        staff_user.id,
        organization.id,
        staff_user.email,
        UserRole.ADMIN,
        uuid4(),
        "csrf",
    )

    resumed = await GmailRecoveryService(db_session).resume_sync(
        principal,
        connector.id,
        expected_version=state.version,
        reason="Gmail credentials were reauthorized.",
    )

    assert resumed.sync_status is GmailSyncStatus.RUNNING
    assert resumed.processed_history_id == "cursor-100"
    assert resumed.pending_history_id == "cursor-105"
    job = await db_session.get(JobIntent, resumed.active_sync_job_id)
    assert job is not None
    assert job.payload["trigger"] == "RECOVERY_RESUME"
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind.in_(("email.classify", "email.draft"))
        )
    ) == downstream_before
    assert await db_session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.action == "gmail.recovery.resume",
            AuditEvent.object_id == state.id,
        )
    ) is not None
