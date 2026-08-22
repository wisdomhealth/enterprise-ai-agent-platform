from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.database import async_sessionmaker, engine
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.drive_gateway import DriveFile
from app.modules.knowledge.models import Document, DriveSource, KnowledgeBase
from app.modules.knowledge.operations import enqueue_drive_sync_intent
from app.modules.knowledge.sync import DriveSyncService


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

    result = await service.sync(source_id, source.sync_cursor)

    db_session.expire_all()
    persisted = await db_session.get(DriveSource, source_id)
    assert result.cursor == "cursor-2"
    assert persisted is not None
    assert persisted.sync_cursor == "cursor-2"
    assert await db_session.scalar(select(func.count(Document.id))) == 1
    assert await db_session.scalar(select(func.count(JobIntent.id))) == 1
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
    first.version += 1
    await db_session.commit()

    second = (await enqueue_drive_sync_intent(db_session, source)).job

    assert second.id != first.id
    assert second.state is JobState.PENDING


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
