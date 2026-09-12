from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from app.core.database import async_sessionmaker
from app.modules.email.tasks import (
    EMAIL_CLASSIFY_KIND,
    EMAIL_DRAFT_KIND,
    EMAIL_HISTORY_KIND,
    _consume_email_job,
)
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobService


async def _seed_job(kind: str) -> UUID:
    async with async_sessionmaker() as session:
        job = await JobService().enqueue(
            session,
            kind,
            f"missing-greenlet:{kind}:{uuid4()}",
            {"work_item_id": str(uuid4()), "connector_id": str(uuid4())},
        )
        job_id = job.id
        await session.commit()
        return job_id


async def _job(job_id: UUID) -> JobIntent:
    async with async_sessionmaker() as session:
        job = await session.get(JobIntent, job_id)
        assert job is not None
        return job


@pytest.mark.asyncio
async def test_history_rollback_does_not_expire_claimed_job_before_complete(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    job_id = await _seed_job(EMAIL_HISTORY_KIND)

    async def rolled_back_history(db_session, _job_id, _payload, _settings):  # type: ignore[no-untyped-def]
        await db_session.rollback()
        return False

    monkeypatch.setattr("app.modules.email.tasks._consume_history", rolled_back_history)
    try:
        await _consume_email_job(job_id)
        persisted = await _job(job_id)
        assert persisted.state is JobState.SUCCEEDED
    finally:
        async with async_sessionmaker() as session:
            await session.execute(JobIntent.__table__.delete().where(JobIntent.id == job_id))
            await session.commit()


@pytest.mark.asyncio
async def test_exception_recovery_uses_fresh_state_and_preserves_business_error(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    job_id = await _seed_job(EMAIL_HISTORY_KIND)

    async def failing_history(db_session, _job_id, _payload, _settings):  # type: ignore[no-untyped-def]
        await db_session.rollback()
        raise RuntimeError("history business failure")

    monkeypatch.setattr("app.modules.email.tasks._consume_history", failing_history)
    try:
        with pytest.raises(RuntimeError, match="history business failure"):
            await _consume_email_job(job_id)
        persisted = await _job(job_id)
        assert persisted.state is JobState.PENDING
        assert persisted.last_error_code == "EMAIL_WORKER_TRANSIENT_FAILURE"
    finally:
        async with async_sessionmaker() as session:
            await session.execute(JobIntent.__table__.delete().where(JobIntent.id == job_id))
            await session.commit()


@pytest.mark.asyncio
async def test_history_completion_reads_current_version_after_lease_generation_changes(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    job_id = await _seed_job(EMAIL_HISTORY_KIND)

    async def renewed_history(_db_session, _job_id, _payload, _settings):  # type: ignore[no-untyped-def]
        async with async_sessionmaker() as renewal_session:
            await renewal_session.execute(
                update(JobIntent)
                .where(JobIntent.id == job_id)
                .values(version=JobIntent.version + 1)
            )
            await renewal_session.commit()
        return False

    monkeypatch.setattr("app.modules.email.tasks._consume_history", renewed_history)
    try:
        await _consume_email_job(job_id)
        persisted = await _job(job_id)
        assert persisted.state is JobState.SUCCEEDED
    finally:
        async with async_sessionmaker() as session:
            await session.execute(JobIntent.__table__.delete().where(JobIntent.id == job_id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "consumer_name"),
    [(EMAIL_CLASSIFY_KIND, "_consume_classification"), (EMAIL_DRAFT_KIND, "_consume_draft")],
)
async def test_non_history_terminal_paths_do_not_read_expired_claimed_job(
    kind: str, consumer_name: str, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    job_id = await _seed_job(kind)

    async def rolled_back_consumer(db_session, _job_id, _payload, _settings):  # type: ignore[no-untyped-def]
        await db_session.rollback()

    monkeypatch.setattr(f"app.modules.email.tasks.{consumer_name}", rolled_back_consumer)
    try:
        await _consume_email_job(job_id)
        assert (await _job(job_id)).state is JobState.SUCCEEDED
    finally:
        async with async_sessionmaker() as session:
            await session.execute(JobIntent.__table__.delete().where(JobIntent.id == job_id))
            await session.commit()
