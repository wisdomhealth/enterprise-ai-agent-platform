import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.database import async_sessionmaker, engine
from app.modules.audit.models import AuditEvent
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.drive_gateway import DriveFile
from app.modules.knowledge.models import (
    Document,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.operations import enqueue_drive_sync_intent
from app.modules.knowledge.sync import DriveSyncService, StaleDriveAssessment
from app.modules.outbox.models import OutboxEvent


class FakeDriveChangeBoundary:
    def __init__(
        self,
        files: list[DriveFile],
        next_cursor: str | None,
        *,
        content: bytes = b"unchanged-content",
        transaction_session=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.files = files
        self.next_cursor = next_cursor
        self.content = content
        self.transaction_session = transaction_session
        self.calls: list[tuple[str, str | None]] = []
        self.download_calls: list[str] = []

    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        self.calls.append((str(source.id), sync_cursor))
        return self.files, self.next_cursor

    async def get_start_page_token(self, _db_session, *, source):  # type: ignore[no-untyped-def]
        self.calls.append((str(source.id), "bootstrap"))
        return "start-cursor"

    async def download(self, file_id: str) -> bytes:
        if self.transaction_session is not None:
            assert not self.transaction_session.in_transaction()
        self.download_calls.append(file_id)
        return self.content

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


def _authorized_file(
    *,
    file_id: str = "file-1",
    name: str = "guide.pdf",
    modified_time: datetime | None = None,
) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=name,
        mime_type="application/pdf",
        modified_time=modified_time or datetime(2026, 8, 22, tzinfo=UTC),
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
    assert await db_session.scalar(
        select(func.count(Document.id)).where(Document.source_id == source_id)
    ) == 1
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["source_id"].astext == str(source_id),
        )
    ) == 1
    parse_events = (
        await db_session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "knowledge.document.parse.requested",
                OutboxEvent.event_id.in_(result.parse_outbox_event_ids),
            )
        )
    ).all()
    assert result.parse_outbox_event_ids == (parse_events[0].event_id,)
    assert parse_events[0].payload["parent_sync_job_id"] == str(parent_sync_job_id)
    assert boundary.calls == [(str(source_id), "cursor-1")]


@pytest.mark.asyncio
async def test_stale_duplicate_change_page_is_discarded(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    source_id = source.id
    boundary = FakeDriveChangeBoundary([_authorized_file()], "cursor-2")
    service = DriveSyncService(db_session, page_gateway=boundary)

    await service.sync(source_id, "cursor-1")
    with pytest.raises(StaleDriveAssessment, match="cursor changed"):
        await service.sync(source_id, "cursor-1")

    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["source_id"].astext == str(source_id),
        )
    ) == 1


@pytest.mark.asyncio
async def test_same_filename_with_new_drive_id_enqueues_a_new_document(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    source_id = source.id

    await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [_authorized_file(file_id="old-drive-id", name="policy.pdf")], "cursor-2"
        ),
    ).sync(source.id, "cursor-1")
    await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [_authorized_file(file_id="new-drive-id", name="policy.pdf")], "cursor-3"
        ),
    ).sync(source.id, "cursor-2")

    documents = list(
        (
            await db_session.scalars(
                select(Document)
                .where(Document.source_id == source_id)
                .order_by(Document.external_id)
            )
        ).all()
    )
    assert [(document.external_id, document.title) for document in documents] == [
        ("new-drive-id", "policy.pdf"),
        ("old-drive-id", "policy.pdf"),
    ]
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["source_id"].astext == str(source_id),
        )
    ) == 2


@pytest.mark.asyncio
async def test_renamed_drive_file_updates_the_existing_document_title(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    content = b"same PDF bytes after rename"

    await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [_authorized_file(file_id="stable-drive-id", name="old-name.pdf")],
            "cursor-2",
        ),
    ).sync(source.id, "cursor-1")
    original = await db_session.scalar(
        select(Document).where(
            Document.source_id == source.id,
            Document.external_id == "stable-drive-id",
        )
    )
    assert original is not None
    original_id = original.id
    version = DocumentVersion(
        document_id=original.id,
        state=DocumentVersionState.RETRIEVABLE,
        content_sha256=sha256(content).hexdigest(),
    )
    db_session.add(version)
    await db_session.flush()
    original.current_version_id = version.id
    original_job = await db_session.scalar(
        select(JobIntent).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["document_id"].astext == str(original.id),
        )
    )
    assert original_job is not None
    original_job.state = JobState.SUCCEEDED
    await db_session.commit()

    rename_boundary = FakeDriveChangeBoundary(
        [
            _authorized_file(
                file_id="stable-drive-id",
                name="renamed-policy.pdf",
                modified_time=datetime(2026, 8, 23, tzinfo=UTC),
            )
        ],
        "cursor-3",
        content=content,
        transaction_session=db_session,
    )
    result = await DriveSyncService(
        db_session,
        page_gateway=rename_boundary,
    ).sync(source.id, "cursor-2")

    documents = list(
        (
            await db_session.scalars(
                select(Document).where(Document.source_id == source.id)
            )
        ).all()
    )
    assert [(document.id, document.external_id, document.title) for document in documents] == [
        (original_id, "stable-drive-id", "renamed-policy.pdf")
    ]
    assert result.enqueued_documents == 0
    assert result.parse_outbox_event_ids == ()
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["source_id"].astext == str(source.id),
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(OutboxEvent.event_id)).where(
            OutboxEvent.event_type == "knowledge.document.parse.requested",
            OutboxEvent.payload["source_id"].astext == str(source.id),
        )
    ) == 1
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.organization_id == source.organization_id,
            AuditEvent.action == "knowledge.document.sync.content_unchanged",
            AuditEvent.object_id == original_id,
        )
    )
    assert audit is not None
    assert audit.outcome == "SUCCESS"
    assert audit.details["version_state"] == DocumentVersionState.RETRIEVABLE.value
    assert audit.details["parse_decision"] == "SKIPPED_CONTENT_UNCHANGED"
    assert audit.details["previous_title"] == "old-name.pdf"
    assert audit.details["current_title"] == "renamed-policy.pdf"
    assert rename_boundary.download_calls == ["stable-drive-id"]


@pytest.mark.asyncio
async def test_changed_content_still_enqueues_a_successor_parse_job(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    old_content = b"old file content"
    await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [_authorized_file(file_id="changed-file", name="old-name.pdf")],
            "cursor-2",
        ),
    ).sync(source.id, "cursor-1")
    document = await db_session.scalar(
        select(Document).where(
            Document.source_id == source.id,
            Document.external_id == "changed-file",
        )
    )
    assert document is not None
    current = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.RETRIEVABLE,
        content_sha256=sha256(old_content).hexdigest(),
    )
    db_session.add(current)
    await db_session.flush()
    document.current_version_id = current.id
    original_job = await db_session.scalar(
        select(JobIntent).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["document_id"].astext == str(document.id),
        )
    )
    assert original_job is not None
    original_job.state = JobState.SUCCEEDED
    await db_session.commit()

    result = await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [
                _authorized_file(
                    file_id="changed-file",
                    name="new-name.pdf",
                    modified_time=datetime(2026, 8, 23, tzinfo=UTC),
                )
            ],
            "cursor-3",
            content=b"new file content",
            transaction_session=db_session,
        ),
    ).sync(source.id, "cursor-2")

    await db_session.refresh(document)
    assert document.title == "new-name.pdf"
    assert document.current_version_id == current.id
    assert result.enqueued_documents == 1
    assert len(result.parse_outbox_event_ids) == 1
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["document_id"].astext == str(document.id),
        )
    ) == 2


@pytest.mark.asyncio
async def test_renamed_file_with_matching_processing_version_reuses_original_job(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    content = b"content already checkpointed by original parse"
    await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [_authorized_file(file_id="processing-file", name="old-name.pdf")],
            "cursor-2",
        ),
    ).sync(source.id, "cursor-1")
    document = await db_session.scalar(
        select(Document).where(
            Document.source_id == source.id,
            Document.external_id == "processing-file",
        )
    )
    assert document is not None
    original_job = await db_session.scalar(
        select(JobIntent).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["document_id"].astext == str(document.id),
        )
    )
    assert original_job is not None
    processing = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.PROCESSING,
        content_sha256=sha256(content).hexdigest(),
    )
    db_session.add(processing)
    await db_session.flush()
    original_job.payload = {
        **original_job.payload,
        "document_version_id": str(processing.id),
    }
    await db_session.commit()

    rename_boundary = FakeDriveChangeBoundary(
        [
            _authorized_file(
                file_id="processing-file",
                name="renamed-during-processing.pdf",
                modified_time=datetime(2026, 8, 23, tzinfo=UTC),
            )
        ],
        "cursor-3",
        content=content,
    )
    result = await DriveSyncService(
        db_session,
        page_gateway=rename_boundary,
    ).sync(source.id, "cursor-2")

    await db_session.refresh(document)
    await db_session.refresh(original_job)
    await db_session.refresh(processing)
    assert document.title == "renamed-during-processing.pdf"
    assert document.current_version_id is None
    assert processing.state is DocumentVersionState.PROCESSING
    assert original_job.state is JobState.PENDING
    assert result.enqueued_documents == 0
    assert result.parse_outbox_event_ids == ()
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["document_id"].astext == str(document.id),
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(OutboxEvent.event_id)).where(
            OutboxEvent.event_type == "knowledge.document.parse.requested",
            OutboxEvent.payload["document_id"].astext == str(document.id),
        )
    ) == 1
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "knowledge.document.sync.content_unchanged",
            AuditEvent.object_id == document.id,
        )
    )
    assert audit is not None
    assert audit.outcome == "DEFERRED"
    assert audit.details["version_state"] == DocumentVersionState.PROCESSING.value
    assert audit.details["parse_decision"] == "RECOVER_EXISTING_JOB"
    assert audit.details["parse_job_id"] == str(original_job.id)
    assert rename_boundary.download_calls == ["processing-file"]


@pytest.mark.asyncio
async def test_orphan_processing_version_records_reason_without_duplicate_job(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session)
    content = b"orphan processing checkpoint"
    await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [_authorized_file(file_id="orphan-file", name="old-name.pdf")],
            "cursor-2",
        ),
    ).sync(source.id, "cursor-1")
    document = await db_session.scalar(
        select(Document).where(
            Document.source_id == source.id,
            Document.external_id == "orphan-file",
        )
    )
    assert document is not None
    original_job = await db_session.scalar(
        select(JobIntent).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["document_id"].astext == str(document.id),
        )
    )
    assert original_job is not None
    processing = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.PROCESSING,
        content_sha256=sha256(content).hexdigest(),
    )
    db_session.add(processing)
    original_job.state = JobState.SUCCEEDED
    await db_session.commit()

    result = await DriveSyncService(
        db_session,
        page_gateway=FakeDriveChangeBoundary(
            [
                _authorized_file(
                    file_id="orphan-file",
                    name="renamed-orphan.pdf",
                    modified_time=datetime(2026, 8, 23, tzinfo=UTC),
                )
            ],
            "cursor-3",
            content=content,
        ),
    ).sync(source.id, "cursor-2")

    await db_session.refresh(document)
    await db_session.refresh(processing)
    assert document.title == "renamed-orphan.pdf"
    assert document.current_version_id is None
    assert processing.state is DocumentVersionState.PROCESSING
    assert result.enqueued_documents == 0
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "knowledge.document.parse",
            JobIntent.payload["document_id"].astext == str(document.id),
        )
    ) == 1
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "knowledge.document.sync.content_unchanged",
            AuditEvent.object_id == document.id,
        )
    )
    assert audit is not None
    assert audit.outcome == "ATTENTION_REQUIRED"
    assert audit.details["parse_decision"] == "PROCESSING_RECOVERY_UNAVAILABLE"
    assert audit.details["reason"] == "NO_RECOVERABLE_PARSE_JOB"


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
async def test_bootstrap_cursor_is_not_committed_before_page_failure(db_session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(db_session, cursor=None)
    source_id = source.id
    boundary = FailingAfterBootstrapBoundary([], None)

    with pytest.raises(RuntimeError, match="temporary Drive page failure"):
        await DriveSyncService(db_session, page_gateway=boundary).sync(source_id)

    db_session.expire_all()
    persisted = await db_session.get(DriveSource, source_id)
    assert persisted is not None
    assert persisted.sync_cursor is None


@pytest.mark.asyncio
async def test_bootstrap_cursor_rolls_back_with_page_failure_in_a_new_database_session() -> None:
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
        assert persisted.sync_cursor is None
        organization = await session_b.get(Organization, organization_id)
        assert organization is not None
        await session_b.delete(organization)
        await session_b.commit()
    await engine.dispose()
