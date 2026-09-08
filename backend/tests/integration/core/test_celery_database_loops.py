"""Regression coverage for synchronous Celery tasks using fresh asyncio.run loops."""

from __future__ import annotations

import asyncio

from sqlalchemy import event, text

from app.core.database import celery_async_sessionmaker, celery_engine
from app.modules.chat.tasks import dispatch_pending_chat_answer_jobs


def test_celery_sessions_do_not_retain_asyncpg_connections_between_run_loops() -> None:
    """A worker entrypoint can run repeatedly without cross-loop pool reuse."""
    checkouts = 0
    checkins = 0

    def checked_out(*_args: object) -> None:
        nonlocal checkouts
        checkouts += 1

    def checked_in(*_args: object) -> None:
        nonlocal checkins
        checkins += 1

    event.listen(celery_engine.sync_engine, "checkout", checked_out)
    event.listen(celery_engine.sync_engine, "checkin", checked_in)
    try:
        async def query_once() -> None:
            async with celery_async_sessionmaker() as db_session:
                assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1

        # Each invocation emulates Celery's synchronous shared_task wrapper.
        for _ in range(3):
            asyncio.run(query_once())
            dispatch_pending_chat_answer_jobs.run()
    finally:
        event.remove(celery_engine.sync_engine, "checkout", checked_out)
        event.remove(celery_engine.sync_engine, "checkin", checked_in)

    assert checkouts == checkins
    assert checkouts == 6
