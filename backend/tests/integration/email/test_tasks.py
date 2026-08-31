from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.models import EmailSyncState
from app.modules.email.tasks import _enqueue_gmail_history_jobs
from app.modules.jobs.models import JobIntent, JobState


@pytest.mark.asyncio
async def test_each_completed_history_poll_gets_a_new_durable_job_key(
    db_session: AsyncSession, email_context: dict[str, object], monkeypatch
) -> None:
    dispatched: list[UUID] = []
    monkeypatch.setattr(
        "app.modules.email.tasks.email_job.delay",
        lambda job_id: dispatched.append(UUID(job_id)),
    )

    for expected_count in (1, 2, 3):
        await _enqueue_gmail_history_jobs(db_session=db_session)
        jobs = list(
            (
                await db_session.scalars(
                    select(JobIntent)
                    .where(JobIntent.kind == "email.gmail_history")
                    .order_by(JobIntent.created_at, JobIntent.id)
                )
            ).all()
        )
        assert len(jobs) == expected_count
        dispatched_job = await db_session.get(JobIntent, dispatched[-1])
        assert dispatched_job is not None
        dispatched_job.state = JobState.SUCCEEDED
        dispatched_job.version = 3
        await db_session.commit()

    assert len(set(job.idempotency_key for job in jobs)) == 3
    assert set(dispatched) == {job.id for job in jobs}


@pytest.mark.asyncio
async def test_history_job_key_is_bounded_for_provider_page_tokens(
    db_session: AsyncSession, email_context: dict[str, object], monkeypatch
) -> None:
    connector = email_context["connector"]
    db_session.add(
        EmailSyncState(
            organization_id=connector.organization_id,
            connector_id=connector.id,
            history_id="cursor-101",
            pending_page_token=f"history:{'x' * 900}",
        )
    )
    await db_session.commit()
    monkeypatch.setattr("app.modules.email.tasks.email_job.delay", lambda _job_id: None)

    await _enqueue_gmail_history_jobs(db_session=db_session)

    job = await db_session.scalar(
        select(JobIntent).where(JobIntent.kind == "email.gmail_history")
    )
    assert job is not None
    assert len(job.idempotency_key) <= 255
