from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

import app.modules.jobs.service as jobs_service_module
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
async def test_database_clock_controls_job_lease_transitions(
    monkeypatch, job_service, lease_service, db_session
):
    claim_job = await job_service.enqueue(
        db_session, "drive.sync", "drive:database-clock:claim", {"cursor": "1"}
    )
    retry_job = await job_service.enqueue(
        db_session, "drive.sync", "drive:database-clock:retry", {"cursor": "2"}
    )
    terminal_job = await job_service.enqueue(
        db_session, "drive.sync", "drive:database-clock:terminal", {"cursor": "3"}
    )
    await db_session.flush()
    await lease_service.claim(retry_job.id, "worker-retry", 60)
    await lease_service.claim(terminal_job.id, "worker-terminal", 60)

    class WorkerClockMustNotBeRead:
        @classmethod
        def now(cls, *_args, **_kwargs):
            raise AssertionError("job leases must use PostgreSQL CURRENT_TIMESTAMP")

    monkeypatch.setattr(
        jobs_service_module,
        "datetime",
        WorkerClockMustNotBeRead,
        raising=False,
    )

    claimed = await lease_service.claim(claim_job.id, "worker-claim", 60)
    assert claimed is not None
    assert await lease_service.claim(claim_job.id, "worker-other", 60) is None
    completed = await lease_service.complete(claim_job.id, "worker-claim")
    retried = await lease_service.retry(
        retry_job.id,
        "worker-retry",
        error_code="rate_limited",
        error_class=ErrorClass.RETRYABLE,
    )
    failed = await lease_service.fail_terminal(
        terminal_job.id,
        "worker-terminal",
        error_code="invalid_request",
    )
    database_now = await db_session.scalar(select(func.current_timestamp()))

    assert completed.state is JobState.SUCCEEDED
    assert retried.state is JobState.PENDING
    assert retried.next_attempt_at is not None
    assert database_now is not None
    assert retried.next_attempt_at > database_now
    assert failed.state is JobState.FAILED


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
@pytest.mark.parametrize("worker_method", ["handle_failure", "manual_retry"])
async def test_job_worker_security_failure_transitions_and_audits_safely(
    worker_method, job_service, lease_service, db_session
):
    organization_id = uuid4()
    actor_id = uuid4()
    job = await job_service.enqueue(
        db_session,
        "drive.sync",
        f"drive:worker-security:{worker_method}",
        {"access_token": "must-not-be-audited"},
    )
    await db_session.flush()
    assert await lease_service.claim(job.id, "worker-security", 60) is not None
    worker = JobWorker(lease_service)

    result = await getattr(worker, worker_method)(
        job.id,
        "worker-security",
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
    assert result.state is JobState.FAILED
    assert audit_event is not None
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
    organization_id = uuid4()
    actor_id = uuid4()
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
        organization_id=organization_id,
        actor_id=actor_id,
        retry_after_seconds=10,
    )
    manual_result = await worker.manual_retry(
        manual.id,
        "worker-b",
        error_code="rate_limited",
        error_class=ErrorClass.RETRYABLE,
        organization_id=organization_id,
        actor_id=actor_id,
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
