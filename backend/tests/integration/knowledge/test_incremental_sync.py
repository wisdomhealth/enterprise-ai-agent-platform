import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.database import async_sessionmaker, engine
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.drive_gateway import DriveFile
from app.modules.knowledge.models import Document, DriveSource, KnowledgeBase
from app.modules.knowledge.operations import enqueue_drive_sync_intent
from app.modules.knowledge.sync import DriveSyncService
from app.modules.outbox.models import OutboxEvent


class FakeDriveChangeBoundary:
    def __init__(self, files: list[DriveFile], next_cursor: str | None) -> None:
        self.files = files
        self.next_cursor = next_cursor
        self.calls: list[tuple[str, str | None]] = []

    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        self.calls.append((str(source.id), sync_cursor))
        return self.files, self.next_cursor

    async def get_start_page_token(self, _db_session, *, source):  # type: ignore[no-untyped-def]
        self.calls.append((str(source.id), "bootstrap"))
        return "start-cursor"

    @staticmethod
    def is_file_authorized(source, drive_file):  # type: ignore[no-untyped-def]
        return source.root_folder_id in drive_file.parent_ids


class FailingAfterBootstrapBoundary(FakeDriveChangeBoundary):
    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        raise RuntimeError("temporary Drive page failure")


async def _source(db_session, *, cursor: str | None = "cursor-1") -> DriveSource:  # type: ignore[no-untyped-def]
    organization = Organization(name="Incremental sync owner")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="root",
        allowed_descendant_ids=[],
        connection_identity="reader@example.test",
        sync_cursor=cursor,
    )
    db_session.add(source)
    await db_session.commit()
    return source


def _authorized_file() -> DriveFile:
    return DriveFile(
        id="file-1",
        name="guide.pdf",
        mime_type="application/pdf",
        modified_time=datetime(2026, 8, 22, tzinfo=UTC),
        parent_ids=("root",),
        web_view_link=None,
        removed=False,
    )


@pytest.mark.asyncio
async def test_cursor_advances_only_after_page_is_persisted(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    source_id = source.id
    boundary = FakeDriveChangeBoundary([_authorized_file()], "cursor-2")
    service = DriveSyncService(db_session, page_gateway=boundary)

    parent_sync_job_id = uuid4()
    result = await service.sync(
        source_id,
        source.sync_cursor,
        parent_sync_job_id=parent_sync_job_id,
    )

    db_session.expire_all()
    persisted = await db_session.get(DriveSource, source_id)
    assert result.cursor == "cursor-2"
    assert persisted is not None
    assert persisted.sync_cursor == "cursor-2"
    assert await db_session.scalar(select(func.count(Document.id))) == 1
    assert await db_session.scalar(select(func.count(JobIntent.id))) == 1
    parse_events = (
        await db_session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "knowledge.document.parse.requested"
            )
        )
    ).all()
    assert result.parse_outbox_event_ids == (parse_events[0].event_id,)
    assert parse_events[0].payload["parent_sync_job_id"] == str(parent_sync_job_id)
    assert boundary.calls == [(str(source_id), "cursor-1")]


@pytest.mark.asyncio
async def test_duplicate_change_page_creates_one_parse_intent(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    boundary = FakeDriveChangeBoundary([_authorized_file()], "cursor-2")
    service = DriveSyncService(db_session, page_gateway=boundary)

    await service.sync(source.id, "cursor-1")
    await service.sync(source.id, "cursor-1")

    assert await db_session.scalar(select(func.count(JobIntent.id))) == 1


@pytest.mark.asyncio
async def test_first_sync_bootstraps_and_persists_a_drive_cursor(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session, cursor=None)
    source_id = source.id
    boundary = FakeDriveChangeBoundary([_authorized_file()], "cursor-2")

    await DriveSyncService(db_session, page_gateway=boundary).sync(source_id)

    db_session.expire_all()
    persisted = await db_session.get(DriveSource, source_id)
    assert persisted is not None
    assert persisted.sync_cursor == "cursor-2"
    assert boundary.calls == [(str(source_id), "bootstrap"), (str(source_id), "start-cursor")]


@pytest.mark.asyncio
async def test_next_periodic_run_uses_a_new_intent_after_prior_success(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    first = (await enqueue_drive_sync_intent(db_session, source)).job
    first.state = JobState.SUCCEEDED
    await db_session.commit()

    second = (await enqueue_drive_sync_intent(db_session, source)).job

    assert second.id != first.id
    assert second.state is JobState.PENDING
    assert first.idempotency_key == f"knowledge-drive-sync:{source.id}:cursor-1"
    assert second.idempotency_key == f"{first.idempotency_key}:run:{first.id}"


@pytest.mark.asyncio
async def test_terminal_jobs_with_matching_versions_still_create_a_distinct_successor(
) -> None:  # type: ignore[no-untyped-def]
    async with async_sessionmaker() as setup_session:
        source = await _source(setup_session)
        source_id = source.id
        organization_id = source.organization_id

    async with async_sessionmaker() as first_session:
        source = await first_session.get(DriveSource, source_id)
        assert source is not None
        first = (await enqueue_drive_sync_intent(first_session, source)).job
        first.state = JobState.SUCCEEDED
        first.version = 3
        await first_session.commit()
        first_id = first.id

    async with async_sessionmaker() as second_session:
        source = await second_session.get(DriveSource, source_id)
        assert source is not None
        second = (await enqueue_drive_sync_intent(second_session, source)).job
        second.state = JobState.SUCCEEDED
        second.version = 3
        await second_session.commit()
        second_id = second.id
        second_key = second.idempotency_key

    async with async_sessionmaker() as third_session:
        source = await third_session.get(DriveSource, source_id)
        assert source is not None
        third = (await enqueue_drive_sync_intent(third_session, source)).job
        await third_session.commit()
        third_id = third.id
        third_key = third.idempotency_key

    assert len({first_id, second_id, third_id}) == 3
    assert second_key.endswith(f":run:{first_id}")
    assert third_key.endswith(f":run:{second_id}")

    async with async_sessionmaker() as cleanup_session:
        organization = await cleanup_session.get(Organization, organization_id)
        assert organization is not None
        await cleanup_session.delete(organization)
        await cleanup_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    (JobState.PENDING, JobState.RUNNING, JobState.RECONCILIATION),
)
async def test_active_sync_intent_is_reused(db_session, state: JobState) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    first = (await enqueue_drive_sync_intent(db_session, source)).job
    first.state = state
    await db_session.commit()

    second = (await enqueue_drive_sync_intent(db_session, source)).job

    assert second.id == first.id


@pytest.mark.asyncio
async def test_concurrent_enqueue_from_one_terminal_predecessor_creates_one_successor(
) -> None:  # type: ignore[no-untyped-def]
    async with async_sessionmaker() as setup_session:
        source = await _source(setup_session)
        source_id = source.id
        organization_id = source.organization_id
        first = (await enqueue_drive_sync_intent(setup_session, source)).job
        first.state = JobState.SUCCEEDED
        await setup_session.commit()
        first_id = first.id
        first_key = first.idempotency_key

    async def enqueue_once():  # type: ignore[no-untyped-def]
        async with async_sessionmaker() as session:
            current_source = await session.get(DriveSource, source_id)
            assert current_source is not None
            enqueued = await enqueue_drive_sync_intent(session, current_source)
            await session.commit()
            return enqueued.job.id

    first_successor_id, second_successor_id = await asyncio.gather(
        enqueue_once(), enqueue_once()
    )

    assert first_successor_id == second_successor_id
    async with async_sessionmaker() as verification_session:
        jobs = (
            await verification_session.scalars(
                select(JobIntent).where(
                    JobIntent.kind == "knowledge.drive_source.sync",
                    (JobIntent.idempotency_key == first_key)
                    | JobIntent.idempotency_key.like(f"{first_key}:run:%"),
                )
            )
        ).all()
    successors = [job for job in jobs if job.id != first_id]
    assert len(successors) == 1
    assert successors[0].idempotency_key == f"{first_key}:run:{first_id}"

    async with async_sessionmaker() as cleanup_session:
        organization = await cleanup_session.get(Organization, organization_id)
        assert organization is not None
        await cleanup_session.delete(organization)
        await cleanup_session.commit()


@pytest.mark.asyncio
async def test_bootstrap_cursor_is_committed_before_page_failure(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session, cursor=None)
    source_id = source.id
    boundary = FailingAfterBootstrapBoundary([], None)

    with pytest.raises(RuntimeError, match="temporary Drive page failure"):
        await DriveSyncService(db_session, page_gateway=boundary).sync(source_id)

    db_session.expire_all()
    persisted = await db_session.get(DriveSource, source_id)
    assert persisted is not None
    assert persisted.sync_cursor == "start-cursor"


@pytest.mark.asyncio
async def test_bootstrap_cursor_survives_page_failure_in_a_new_database_session() -> None:
    async with async_sessionmaker() as session_a:
        organization = Organization(name="Cross-session bootstrap owner")
        session_a.add(organization)
        await session_a.flush()
        knowledge_base = KnowledgeBase(organization_id=organization.id)
        session_a.add(knowledge_base)
        await session_a.flush()
        source = DriveSource(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            root_folder_id="root",
            allowed_descendant_ids=[],
            connection_identity="reader@example.test",
        )
        session_a.add(source)
        await session_a.commit()
        source_id = source.id
        organization_id = organization.id
        with pytest.raises(RuntimeError, match="temporary Drive page failure"):
            await DriveSyncService(
                session_a, page_gateway=FailingAfterBootstrapBoundary([], None)
            ).sync(source_id)

    async with async_sessionmaker() as session_b:
        persisted = await session_b.get(DriveSource, source_id)
        assert persisted is not None
        assert persisted.sync_cursor == "start-cursor"
        organization = await session_b.get(Organization, organization_id)
        assert organization is not None
        await session_b.delete(organization)
        await session_b.commit()
    await engine.dispose()
