from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.telemetry import prometheus_payload, refresh_operational_metrics
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent, JobState
from app.modules.operations.health import ConfiguredHealthReporter, DependencyStatus
from app.modules.retention.models import ErasureRequest, ErasureScope, ErasureStatus


def _metric_value(payload: str, name: str) -> float:
    prefix = f"{name} "
    return float(
        next(line.removeprefix(prefix) for line in payload.splitlines() if line.startswith(prefix))
    )


@pytest.mark.asyncio
async def test_readiness_erasure_gate_and_metrics_are_cross_session_durable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    key_path = tmp_path / "connector.key"
    key_path.write_bytes(b"k" * 32)
    settings = Settings.model_validate(
        {
            "DATABASE_URL": database_url,
            "APP_ENV": "development",
            "CONNECTOR_FILE_KEY_PATH": key_path,
            "RESTORE_GENERATION": 2,
        }
    )
    reporter = ConfiguredHealthReporter(settings, sessions, redis_client=None)

    organization_id = uuid4()
    user_id = uuid4()
    erasure_id = uuid4()
    job_id = uuid4()
    organization = Organization(id=organization_id, name=f"Task 24 {uuid4()}")
    user = StaffUser(
        id=user_id,
        organization_id=organization_id,
        oidc_subject=f"task24-{uuid4()}",
        email=f"task24-{uuid4()}@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    erasure = ErasureRequest(
        id=erasure_id,
        organization_id=organization_id,
        requested_by_id=user_id,
        subject_key_hash="a" * 64,
        scope=ErasureScope.CUSTOMER,
        status=ErasureStatus.PENDING,
        replay_generation=1,
        verification_counts={},
    )
    job = JobIntent(
        id=job_id,
        kind="task24.metrics",
        idempotency_key=str(uuid4()),
        payload={},
        state=JobState.PENDING,
        attempts=3,
    )

    try:
        async with sessions() as baseline_session:
            await refresh_operational_metrics(baseline_session, restore_generation=2)
        baseline_metrics = prometheus_payload()[0].decode("utf-8")
        baseline_backlog = _metric_value(baseline_metrics, "platform_job_backlog")
        baseline_retries = _metric_value(baseline_metrics, "platform_job_retries")
        baseline_erasure = _metric_value(baseline_metrics, "platform_erasure_backlog")

        async with sessions() as first_session:
            first_session.add_all((organization, user, erasure, job))
            await first_session.commit()

        blocked = await reporter()
        assert blocked.dependencies["database"].status is DependencyStatus.UP
        assert blocked.dependencies["migrations"].status is DependencyStatus.UP
        assert blocked.dependencies["erasure_replay"].status is DependencyStatus.DOWN
        assert blocked.ready is False

        async with sessions() as metrics_session:
            await refresh_operational_metrics(metrics_session, restore_generation=2)
        metrics = prometheus_payload()[0].decode("utf-8")
        assert _metric_value(metrics, "platform_job_backlog") == baseline_backlog + 1
        assert _metric_value(metrics, "platform_job_retries") == baseline_retries + 2
        assert _metric_value(metrics, "platform_erasure_backlog") == baseline_erasure + 1

        async with sessions() as completion_session:
            persisted_erasure = await completion_session.get(ErasureRequest, erasure.id)
            persisted_job = await completion_session.get(JobIntent, job.id)
            assert persisted_erasure is not None and persisted_job is not None
            persisted_erasure.status = ErasureStatus.APPLIED
            persisted_erasure.replay_generation = 2
            persisted_job.state = JobState.SUCCEEDED
            await completion_session.commit()

        recovered = await reporter()
        assert recovered.ready is True
        assert recovered.status == "degraded"
        assert recovered.dependencies["erasure_replay"].status is DependencyStatus.UP

        async with sessions() as fresh_metrics_session:
            await refresh_operational_metrics(fresh_metrics_session, restore_generation=2)
        recovered_metrics = prometheus_payload()[0].decode("utf-8")
        assert _metric_value(recovered_metrics, "platform_job_backlog") == baseline_backlog
        assert _metric_value(recovered_metrics, "platform_erasure_backlog") == baseline_erasure
    finally:
        async with sessions() as cleanup_session:
            await cleanup_session.execute(
                delete(ErasureRequest).where(ErasureRequest.id == erasure_id)
            )
            await cleanup_session.execute(delete(StaffUser).where(StaffUser.id == user_id))
            await cleanup_session.execute(
                delete(Organization).where(Organization.id == organization_id)
            )
            await cleanup_session.execute(delete(JobIntent).where(JobIntent.id == job_id))
            await cleanup_session.commit()
        await engine.dispose()
