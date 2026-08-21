from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.modules.jobs.models import JobIntent
from app.modules.jobs.service import JobService
from app.modules.knowledge.sync import drive_sync_job_key


@pytest.mark.asyncio
async def test_duplicate_manual_retries_preserve_one_durable_intent(db_session) -> None:  # type: ignore[no-untyped-def]
    source_id = uuid4()
    key = drive_sync_job_key(source_id, "cursor-1")
    service = JobService()

    first = await service.enqueue(
        db_session,
        "knowledge.drive_source.sync",
        key,
        {"source_id": str(source_id), "page_token": "cursor-1"},
    )
    second = await service.enqueue(
        db_session,
        "knowledge.drive_source.sync",
        key,
        {"source_id": str(source_id), "page_token": "cursor-1"},
    )
    await db_session.commit()

    assert first.id == second.id
    assert await db_session.scalar(select(func.count(JobIntent.id))) == 1


def test_manual_and_scheduled_sync_share_one_durable_intent_key() -> None:
    assert drive_sync_job_key("source", "cursor") == drive_sync_job_key("source", "cursor")
