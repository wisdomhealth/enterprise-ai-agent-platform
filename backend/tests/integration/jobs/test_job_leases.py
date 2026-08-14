from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.modules.audit.models import AuditEvent
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService, RetryPolicy
from app.modules.jobs.worker import JobWorker


@pytest.fixture
def job_service() -> JobService:
    return JobService()


@pytest.fixture
def lease_service(db_session) -> JobLeaseService:
    return JobLeaseService(db_session, retry_policy=RetryPolicy(jitter=lambda: 0.0))


@pytest.mark.asyncio
async def test_only_one_worker_holds_a_live_job_lease(
    job_service, lease_service, db_session
):
    job = await job_service.enqueue(
        db_session, "drive.sync", "drive:1:cursor:9", {"source_id": "1"}
    )
    await db_session.flush()

    first = await lease_service.claim(job.id, "worker-a", 60)
    second = await lease_service.claim(job.id, "worker-b", 60)

    assert first is not None
    assert first.lease_owner == "worker-a"
    assert second is None


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_without_losing_payload(
    job_service, lease_service, db_session
):
    job = await job_service.enqueue(
        db_session, "drive.sync", "drive:1:cursor:expired", {"cursor": "9"}
    )
    await db_session.flush()
    assert await lease_service.claim(job.id, "dead-worker", 60) is not None
    await db_session.execute(
        update(JobIntent)
        .where(JobIntent.id == job.id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    recovered = await lease_service.claim(job.id, "recovery-worker", 60)

    assert recovered is not None
    assert recovered.lease_owner == "recovery-worker"
    assert recovered.payload == {"cursor": "9"}
    assert recovered.attempts == 2


@pytest.mark.asyncio
async def test_duplicate_enqueue_returns_the_existing_job(job_service, db_session):
    first = await job_service.enqueue(
        db_session, "drive.sync", "drive:1:cursor:duplicate", {"cursor": "9"}
    )
    second = await job_service.enqueue(
        db_session, "drive.sync", "drive:1:cursor:duplicate", {"cursor": "9"}
    )

    assert second.id == first.id


@pytest.mark.asyncio
async def test_database_rejects_duplicate_job_idempotency_binding(db_session):
    db_session.add_all(
        [
            JobIntent(
                kind="drive.sync",
                idempotency_key="drive:unique",
                payload={"cursor": "1"},
            ),
            JobIntent(
                kind="drive.sync",
                idempotency_key="drive:unique",
                payload={"cursor": "2"},
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.flush()


@pytest.mark.asyncio
async def test_completed_job_cannot_be_reclaimed(job_service, lease_service, db_session):
    job = await job_service.enqueue(
        db_session, "drive.sync", "drive:complete", {"cursor": "9"}
    )
    await db_session.flush()
    assert await lease_service.claim(job.id, "worker-a", 60) is not None

    completed = await lease_service.complete(job.id, "worker-a")
    duplicate_completion = await lease_service.complete(job.id, "worker-a")

    assert completed.state is JobState.SUCCEEDED
    assert duplicate_completion.state is JobState.SUCCEEDED
    assert await lease_service.claim(job.id, "worker-b", 60) is None


@pytest.mark.asyncio
async def test_live_lease_owner_is_required_to_complete(
    job_service, lease_service, db_session
):
    job = await job_service.enqueue(
        db_session, "drive.sync", "drive:owner", {"cursor": "9"}
    )
    await db_session.flush()
    assert await lease_service.claim(job.id, "worker-a", 60) is not None

    with pytest.raises(JobLeaseLost):
        await lease_service.complete(job.id, "worker-b")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_class", "expected_state"),
    [
        (ErrorClass.RETRYABLE, JobState.PENDING),
        (ErrorClass.NON_RETRYABLE, JobState.FAILED),
        (ErrorClass.AMBIGUOUS, JobState.RECONCILIATION),
        (ErrorClass.SECURITY, JobState.FAILED),
    ],
)
async def test_retry_routes_each_error_class(
    error_class, expected_state, job_service, lease_service, db_session
):
    job = await job_service.enqueue(
        db_session,
        "drive.sync",
        f"drive:error:{error_class.value}",
        {"cursor": "9"},
    )
    await db_session.flush()
    assert await lease_service.claim(job.id, "worker-a", 60) is not None

    retried = await lease_service.retry(
        job.id,
        "worker-a",
        error_code="provider_error",
        error_class=error_class,
        retry_after_seconds=17,
        organization_id=uuid4(),
        actor_id=uuid4(),
    )

    assert retried.state is expected_state
    assert retried.lease_owner is None
    assert retried.lease_expires_at is None
    assert retried.last_error_code == "provider_error"
    assert retried.error_class is error_class
    if error_class is ErrorClass.RETRYABLE:
        assert retried.next_attempt_at >= datetime.now(UTC) + timedelta(seconds=15)
    else:
        assert retried.next_attempt_at is None


@pytest.mark.asyncio
async def test_security_error_emits_safe_audit_signal(
    job_service, lease_service, db_session
):
    organization_id = uuid4()
    actor_id = uuid4()
    job = await job_service.enqueue(
        db_session, "drive.sync", "drive:security", {"access_token": "secret"}
    )
    await db_session.flush()
    assert await lease_service.claim(job.id, "worker-a", 60) is not None

    await lease_service.retry(
        job.id,
        "worker-a",
        error_code="credential_scope_denied",
        error_class=ErrorClass.SECURITY,
        organization_id=organization_id,
        actor_id=actor_id,
    )

    audit_event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.organization_id == organization_id,
            AuditEvent.actor_id == actor_id,
            AuditEvent.object_id == job.id,
        )
    )
    assert audit_event is not None
    assert audit_event.action == "job.security_denied"
    assert audit_event.details == {"error_code": "credential_scope_denied"}


@pytest.mark.asyncio
async def test_security_error_without_audit_context_does_not_transition_job(
    job_service, lease_service, db_session
):
    job = await job_service.enqueue(
        db_session, "drive.sync", "drive:security-context", {"cursor": "9"}
    )
    await db_session.flush()
    assert await lease_service.claim(job.id, "worker-a", 60) is not None

    with pytest.raises(ValueError, match="organization_id and actor_id"):
        await lease_service.retry(
            job.id,
            "worker-a",
            error_code="credential_scope_denied",
            error_class=ErrorClass.SECURITY,
        )

    await db_session.flush()
    durable_state = await db_session.scalar(
        select(JobIntent.state).where(JobIntent.id == job.id)
    )
    assert durable_state is JobState.RUNNING


@pytest.mark.asyncio
async def test_manual_and_automatic_retry_use_the_same_durable_transition(
    job_service, lease_service, db_session
):
    automatic = await job_service.enqueue(
        db_session, "drive.sync", "drive:auto-retry", {"cursor": "1"}
    )
    manual = await job_service.enqueue(
        db_session, "drive.sync", "drive:manual-retry", {"cursor": "1"}
    )
    await db_session.flush()
    await lease_service.claim(automatic.id, "worker-a", 60)
    await lease_service.claim(manual.id, "worker-b", 60)
    worker = JobWorker(lease_service)

    automatic_result = await worker.handle_failure(
        automatic.id,
        "worker-a",
        error_code="rate_limited",
        error_class=ErrorClass.RETRYABLE,
        retry_after_seconds=10,
    )
    manual_result = await worker.manual_retry(
        manual.id,
        "worker-b",
        error_code="rate_limited",
        error_class=ErrorClass.RETRYABLE,
        retry_after_seconds=10,
    )

    assert automatic_result.state is manual_result.state is JobState.PENDING
    assert automatic_result.last_error_code == manual_result.last_error_code
    assert automatic_result.error_class is manual_result.error_class is ErrorClass.RETRYABLE
    assert automatic_result.next_attempt_at is not None
    assert manual_result.next_attempt_at is not None


@pytest.mark.asyncio
async def test_pending_retry_is_not_claimable_before_next_attempt(
    job_service, lease_service, db_session
):
    job = await job_service.enqueue(
        db_session, "drive.sync", "drive:not-due", {"cursor": "9"}
    )
    await db_session.flush()
    await lease_service.claim(job.id, "worker-a", 60)
    await lease_service.retry(
        job.id,
        "worker-a",
        error_code="rate_limited",
        error_class=ErrorClass.RETRYABLE,
        retry_after_seconds=60,
    )

    assert await lease_service.claim(job.id, "worker-b", 60) is None
