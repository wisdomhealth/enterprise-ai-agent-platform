import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.core.database import async_sessionmaker, engine
from app.modules.audit.models import AuditEvent
from app.modules.connectors.models import (
    Connector,
    ConnectorKind,
    ConnectorSecret,
    ConnectorStatus,
)
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost
from app.modules.knowledge.drive_gateway import (
    DriveFile,
    DriveFileUnavailable,
    DriveUnavailableReason,
)
from app.modules.knowledge.ingestion import DocumentIngestionService
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    DriveSourceStatus,
    KnowledgeBase,
)
from app.modules.knowledge.sync import DriveSyncService, StaleDriveAssessment
from app.modules.knowledge.tasks import _consume_document_cleanup_event
from app.modules.operations.service import OperationsService
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.embeddings import EmbeddingPublicationService


def _file(
    file_id: str,
    *,
    mime_type: str = "application/pdf",
    parents: tuple[str, ...] = ("root",),
    removed: bool = False,
    trashed: bool = False,
) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=f"{file_id}.pdf" if mime_type == "application/pdf" else file_id,
        mime_type=mime_type,
        modified_time=datetime(2026, 10, 8, tzinfo=UTC),
        parent_ids=parents,
        web_view_link=None,
        removed=removed,
        trashed=trashed,
    )


class AssessmentBoundary:
    def __init__(
        self,
        changes: list[DriveFile],
        *,
        descendants: set[str] | None = None,
        current_files: dict[str, DriveFile | Exception | None] | None = None,
        next_cursor: str = "cursor-2",
        descendant_error: Exception | None = None,
    ) -> None:
        self.changes = changes
        self.descendants = descendants or set()
        self.current_files = current_files or {}
        self.next_cursor = next_cursor
        self.descendant_error = descendant_error
        self.calls: list[tuple[str, str]] = []

    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        self.calls.append(("changes", str(sync_cursor)))
        return self.changes, self.next_cursor

    async def get(self, file_id: str) -> DriveFile | None:
        self.calls.append(("get", file_id))
        result = self.current_files.get(file_id)
        if isinstance(result, Exception):
            raise result
        return result

    async def resolve_descendant_folder_ids(self, root_folder_id: str) -> set[str]:
        self.calls.append(("descendants", root_folder_id))
        if self.descendant_error is not None:
            raise self.descendant_error
        return set(self.descendants)


class MutatingAssessmentBoundary(AssessmentBoundary):
    def __init__(self, db_session, source_id, changes):  # type: ignore[no-untyped-def]
        super().__init__(
            changes,
            descendants={"folder-a", "folder-b"},
            current_files={
                "root": _file(
                    "root",
                    mime_type="application/vnd.google-apps.folder",
                    parents=(),
                )
            },
        )
        self.db_session = db_session
        self.source_id = source_id

    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        await self.db_session.execute(
            update(DriveSource)
            .where(DriveSource.id == self.source_id)
            .values(root_folder_id="replacement-root")
        )
        await self.db_session.commit()
        return await super().list_changes(
            _db_session, source=source, sync_cursor=sync_cursor
        )


class FailingAuditService:
    async def record_actor(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("audit persistence failed")


class CredentialRejected(Exception):
    status_code = 401


class RotatingCredentialFailureBoundary:
    def __init__(self, db_session, connector_id, replacement_secret_id):  # type: ignore[no-untyped-def]
        self.db_session = db_session
        self.connector_id = connector_id
        self.replacement_secret_id = replacement_secret_id

    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        await self.db_session.execute(
            update(Connector)
            .where(Connector.id == self.connector_id)
            .values(secret_id=self.replacement_secret_id, status=ConnectorStatus.ACTIVE)
        )
        await self.db_session.commit()
        raise CredentialRejected()


class BlockingEmbeddingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return [[1.0] * 1536 for _ in texts]


async def _source_with_document(
    db_session,  # type: ignore[no-untyped-def]
    *,
    external_id: str = "drive-file",
    version_states: tuple[DocumentVersionState, ...] = (
        DocumentVersionState.RETRIEVABLE,
    ),
) -> tuple[DriveSource, Document, list[DocumentVersion], JobIntent]:
    organization = Organization(name=f"Atomic cleanup {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="root",
        include_descendants=True,
        allowed_descendant_ids=["folder-a", "folder-b"],
        connection_identity="reader@example.test",
        sync_cursor="cursor-1",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id=external_id,
        title="policy.pdf",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    versions: list[DocumentVersion] = []
    for ordinal, state in enumerate(version_states):
        version = DocumentVersion(
            document_id=document.id,
            state=state,
            content_sha256=f"{ordinal + 1:064x}",
        )
        db_session.add(version)
        await db_session.flush()
        versions.append(version)
        db_session.add(
            DocumentChunk(
                id=uuid4(),
                document_version_id=version.id,
                ordinal=0,
                text=f"chunk {ordinal}",
                page_number=1,
                section=None,
                token_count=2,
                metadata_={},
            )
        )
    retrievable = next(
        (version for version in versions if version.state is DocumentVersionState.RETRIEVABLE),
        None,
    )
    document.current_version_id = retrievable.id if retrievable is not None else None
    parse_job = JobIntent(
        kind="knowledge.document.parse",
        idempotency_key=f"parse-{document.id}",
        payload={"source_id": str(source.id), "document_id": str(document.id)},
        state=JobState.RUNNING,
        lease_owner="old-worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db_session.add(parse_job)
    await db_session.commit()
    return source, document, versions, parse_job


@pytest.mark.asyncio
async def test_root_removal_revokes_all_documents_and_deletes_chunks_atomically(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, parse_job = await _source_with_document(db_session)
    source_id = source.id
    document_id = document.id
    parse_job_id = parse_job.id
    version_ids = [version.id for version in versions]
    second_parse_job = JobIntent(
        kind="knowledge.document.parse",
        idempotency_key=f"second-parse-{document_id}",
        payload={"source_id": str(source_id), "document_id": str(document_id)},
        state=JobState.PENDING,
    )
    db_session.add(second_parse_job)
    await db_session.commit()
    second_parse_job_id = second_parse_job.id
    sync_job_id = uuid4()
    boundary = AssessmentBoundary([_file("root", mime_type="", removed=True)])

    result = await DriveSyncService(db_session, page_gateway=boundary).sync(
        source_id,
        "cursor-1",
        parent_sync_job_id=sync_job_id,
    )

    db_session.expire_all()
    persisted_source = await db_session.get(DriveSource, source_id)
    persisted_document = await db_session.get(Document, document_id)
    persisted_job = await db_session.get(JobIntent, parse_job_id)
    persisted_second_job = await db_session.get(JobIntent, second_parse_job_id)
    assert result.root_unavailable_reason == "REMOVED_OR_ACCESS_LOST"
    assert persisted_source is not None
    assert persisted_source.status is DriveSourceStatus.DISABLED
    assert persisted_source.sync_cursor == "cursor-2"
    assert persisted_document is not None
    assert persisted_document.current_version_id is None
    assert persisted_job is not None
    assert persisted_job.state is JobState.FAILED
    assert persisted_second_job is not None
    assert persisted_second_job.state is JobState.FAILED
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.object_id == document_id)
    )
    assert audit is not None
    assert audit.details["reason"] == "AUTHORIZED_ROOT_UNAVAILABLE"
    assert audit.details["root_unavailable_reason"] == "REMOVED_OR_ACCESS_LOST"
    source_audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "knowledge.drive_source.root_unavailable",
            AuditEvent.object_id == source_id,
        )
    )
    assert source_audit is not None
    assert source_audit.outcome == "DISABLED"
    assert source_audit.details["reason"] == "REMOVED_OR_ACCESS_LOST"
    status = await OperationsService(db_session)._source_status(persisted_source)
    assert status.disabled_reason == "ROOT_UNAVAILABLE_REMOVED_OR_ACCESS_LOST"
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id.in_(version_ids)
        )
    ) == 0


@pytest.mark.asyncio
async def test_file_removal_deletes_newly_and_previously_revoked_version_chunks(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(
        db_session,
        version_states=(
            DocumentVersionState.RETRIEVABLE,
            DocumentVersionState.REVOKED,
        ),
    )
    source_id = source.id
    document_id = document.id
    version_ids = [version.id for version in versions]
    sync_job_id = uuid4()
    boundary = AssessmentBoundary(
        [_file(document.external_id, removed=True)],
        descendants={"folder-a", "folder-b"},
        current_files={
            "root": _file("root", mime_type="application/vnd.google-apps.folder", parents=())
        },
    )

    await DriveSyncService(db_session, page_gateway=boundary).sync(
        source_id,
        "cursor-1",
        parent_sync_job_id=sync_job_id,
    )

    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id.in_(version_ids)
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count(OutboxEvent.event_id)).where(
            OutboxEvent.event_type == "knowledge.document.cleanup.requested",
            OutboxEvent.aggregate_id == document_id,
        )
    ) == 0
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "knowledge.document.revocation_cleanup.completed",
            AuditEvent.object_id == document_id,
        )
    )
    assert audit is not None
    assert audit.outcome == "SUCCESS"
    assert audit.details["reason"] == "REMOVED_OR_ACCESS_LOST"
    assert audit.details["deleted_chunk_count"] == 2
    assert audit.details["sync_job_id"] == str(sync_job_id)
    assert audit.details["version_ids"] == sorted(str(value) for value in version_ids)


@pytest.mark.asyncio
async def test_removed_subfolder_rechecks_documents_and_keeps_file_moved_within_root(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    source_id = source.id
    version_id = versions[0].id
    boundary = AssessmentBoundary(
        [_file("folder-a", mime_type="", removed=True)],
        descendants={"folder-b"},
        current_files={
            "root": _file("root", mime_type="application/vnd.google-apps.folder", parents=()),
            document.external_id: _file(document.external_id, parents=("folder-b",)),
        },
    )

    await DriveSyncService(db_session, page_gateway=boundary).sync(source_id, "cursor-1")

    db_session.expire_all()
    persisted_source = await db_session.get(DriveSource, source_id)
    persisted_version = await db_session.get(DocumentVersion, version_id)
    assert persisted_source is not None
    assert persisted_source.allowed_descendant_ids == ["folder-b"]
    assert persisted_version is not None
    assert persisted_version.state is DocumentVersionState.RETRIEVABLE
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) == 1


@pytest.mark.asyncio
async def test_file_moved_outside_authorized_root_is_revoked_and_cleaned(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    source_id = source.id
    version_id = versions[0].id
    boundary = AssessmentBoundary(
        [_file(document.external_id, parents=("outside",))],
        descendants={"folder-a", "folder-b"},
        current_files={
            "root": _file("root", mime_type="application/vnd.google-apps.folder", parents=()),
        },
    )

    await DriveSyncService(db_session, page_gateway=boundary).sync(source_id, "cursor-1")

    persisted_version = await db_session.get(DocumentVersion, version_id)
    assert persisted_version is not None
    assert persisted_version.state is DocumentVersionState.REVOKED
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) == 0
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.object_id == document.id)
    )
    assert audit is not None
    assert audit.details["reason"] == "OUTSIDE_AUTHORIZED_ROOT"


@pytest.mark.asyncio
async def test_changed_file_invalidates_every_older_active_parse_task(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, _, first_job = await _source_with_document(db_session)
    second_job = JobIntent(
        kind="knowledge.document.parse",
        idempotency_key=f"older-generation-{document.id}",
        payload={"source_id": str(source.id), "document_id": str(document.id)},
        state=JobState.PENDING,
    )
    db_session.add(second_job)
    await db_session.commit()
    source_id = source.id
    first_job_id = first_job.id
    second_job_id = second_job.id
    changed_file = _file(document.external_id, parents=("folder-a",))
    boundary = AssessmentBoundary(
        [changed_file],
        descendants={"folder-a", "folder-b"},
        current_files={
            "root": _file(
                "root",
                mime_type="application/vnd.google-apps.folder",
                parents=(),
            )
        },
    )

    await DriveSyncService(db_session, page_gateway=boundary).sync(
        source_id, "cursor-1"
    )

    db_session.expire_all()
    persisted_first = await db_session.get(JobIntent, first_job_id)
    persisted_second = await db_session.get(JobIntent, second_job_id)
    assert persisted_first is not None
    assert persisted_second is not None
    assert persisted_first.state is JobState.FAILED
    assert persisted_second.state is JobState.FAILED
    assert persisted_first.last_error_code == "DOCUMENT_SUPERSEDED"
    assert persisted_second.last_error_code == "DOCUMENT_SUPERSEDED"
    current_key = DriveSyncService._parse_job_key(source_id, changed_file)
    current_job = await db_session.scalar(
        select(JobIntent).where(JobIntent.idempotency_key == current_key)
    )
    assert current_job is not None
    assert current_job.state is JobState.PENDING


@pytest.mark.asyncio
async def test_subfolder_moved_outside_rechecks_and_cleans_its_documents(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    source_id = source.id
    version_id = versions[0].id
    boundary = AssessmentBoundary(
        [
            _file(
                "folder-a",
                mime_type="application/vnd.google-apps.folder",
                parents=("outside",),
            )
        ],
        descendants={"folder-b"},
        current_files={
            "root": _file(
                "root",
                mime_type="application/vnd.google-apps.folder",
                parents=(),
            ),
            document.external_id: _file(
                document.external_id, parents=("folder-a",)
            ),
        },
    )

    await DriveSyncService(db_session, page_gateway=boundary).sync(
        source_id, "cursor-1"
    )

    version = await db_session.get(DocumentVersion, version_id)
    assert version is not None
    assert version.state is DocumentVersionState.REVOKED
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) == 0


@pytest.mark.asyncio
async def test_failed_scope_verification_preserves_data_and_cursor(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    source_id = source.id
    version_id = versions[0].id
    boundary = AssessmentBoundary(
        [_file(document.external_id, removed=True)],
        descendant_error=TimeoutError("Drive folder enumeration timed out"),
        current_files={
            "root": _file("root", mime_type="application/vnd.google-apps.folder", parents=()),
        },
    )

    with pytest.raises(TimeoutError, match="enumeration timed out"):
        await DriveSyncService(db_session, page_gateway=boundary).sync(source_id, "cursor-1")

    await db_session.rollback()
    db_session.expire_all()
    persisted_source = await db_session.get(DriveSource, source_id)
    persisted_version = await db_session.get(DocumentVersion, version_id)
    assert persisted_source is not None
    assert persisted_source.sync_cursor == "cursor-1"
    assert persisted_source.status is DriveSourceStatus.ACTIVE
    assert persisted_version is not None
    assert persisted_version.state is DocumentVersionState.RETRIEVABLE
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        (DriveUnavailableReason.NOT_FOUND_OR_NO_ACCESS, "NOT_FOUND_OR_NO_ACCESS"),
        (DriveUnavailableReason.ACCESS_DENIED, "ACCESS_DENIED"),
    ),
)
async def test_specific_file_lookup_loss_revokes_with_exact_reason(
    db_session, reason: DriveUnavailableReason, expected: str
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    boundary = AssessmentBoundary(
        [_file("folder-a", mime_type="", removed=True)],
        descendants={"folder-b"},
        current_files={
            "root": _file("root", mime_type="application/vnd.google-apps.folder", parents=()),
            document.external_id: DriveFileUnavailable(document.external_id, reason),
        },
    )

    await DriveSyncService(db_session, page_gateway=boundary).sync(source.id, "cursor-1")

    version = await db_session.get(DocumentVersion, versions[0].id)
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.object_id == document.id)
    )
    assert version is not None
    assert version.state is DocumentVersionState.REVOKED
    assert audit is not None
    assert audit.details["reason"] == expected


@pytest.mark.asyncio
async def test_changed_source_discards_assessment_without_cleanup(db_session) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    source_id = source.id
    version_id = versions[0].id
    boundary = MutatingAssessmentBoundary(
        db_session,
        source_id,
        [_file(document.external_id, removed=True)],
    )

    with pytest.raises(StaleDriveAssessment, match="source changed"):
        await DriveSyncService(db_session, page_gateway=boundary).sync(
            source_id, "cursor-1"
        )

    persisted_source = await db_session.get(DriveSource, source_id)
    persisted_version = await db_session.get(DocumentVersion, version_id)
    assert persisted_source is not None
    assert persisted_source.root_folder_id == "replacement-root"
    assert persisted_source.sync_cursor == "cursor-1"
    assert persisted_version is not None
    assert persisted_version.state is DocumentVersionState.RETRIEVABLE
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) == 1


@pytest.mark.asyncio
async def test_cleanup_audit_failure_rolls_back_versions_chunks_and_cursor(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    source_id = source.id
    version_id = versions[0].id
    boundary = AssessmentBoundary(
        [_file(document.external_id, removed=True)],
        descendants={"folder-a", "folder-b"},
        current_files={
            "root": _file(
                "root",
                mime_type="application/vnd.google-apps.folder",
                parents=(),
            )
        },
    )

    with pytest.raises(RuntimeError, match="audit persistence failed"):
        await DriveSyncService(
            db_session,
            page_gateway=boundary,
            audit_service=FailingAuditService(),  # type: ignore[arg-type]
        ).sync(source_id, "cursor-1")

    persisted_source = await db_session.get(DriveSource, source_id)
    persisted_version = await db_session.get(DocumentVersion, version_id)
    assert persisted_source is not None
    assert persisted_source.sync_cursor == "cursor-1"
    assert persisted_version is not None
    assert persisted_version.state is DocumentVersionState.RETRIEVABLE
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) == 1


@pytest.mark.asyncio
async def test_incomplete_change_page_never_triggers_cleanup(db_session) -> None:  # type: ignore[no-untyped-def]
    source, document, versions, _ = await _source_with_document(db_session)
    source_id = source.id
    version_id = versions[0].id
    boundary = AssessmentBoundary(
        [_file(document.external_id, removed=True)],
        next_cursor=None,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="continuation cursor"):
        await DriveSyncService(db_session, page_gateway=boundary).sync(
            source_id, "cursor-1"
        )

    persisted_version = await db_session.get(DocumentVersion, version_id)
    assert persisted_version is not None
    assert persisted_version.state is DocumentVersionState.RETRIEVABLE
    assert await db_session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) == 1


@pytest.mark.asyncio
async def test_old_authorization_failure_cannot_overwrite_rotated_connector(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    source, _, _, _ = await _source_with_document(db_session)
    old_secret = ConnectorSecret(
        organization_id=source.organization_id,
        ciphertext=b"old",
        encrypted_data_key=b"old-key",
        nonce=b"old-nonce",
        algorithm="AES-GCM",
        key_version="old",
    )
    replacement_secret = ConnectorSecret(
        organization_id=source.organization_id,
        ciphertext=b"new",
        encrypted_data_key=b"new-key",
        nonce=b"new-nonce",
        algorithm="AES-GCM",
        key_version="new",
    )
    db_session.add_all((old_secret, replacement_secret))
    await db_session.flush()
    connector = Connector(
        organization_id=source.organization_id,
        kind=ConnectorKind.DRIVE,
        status=ConnectorStatus.ACTIVE,
        secret_id=old_secret.id,
    )
    db_session.add(connector)
    await db_session.commit()
    source_id = source.id
    connector_id = connector.id
    replacement_secret_id = replacement_secret.id

    result = await DriveSyncService(
        db_session,
        page_gateway=RotatingCredentialFailureBoundary(
            db_session, connector_id, replacement_secret_id
        ),
    ).sync(source_id, "cursor-1")

    persisted_source = await db_session.get(DriveSource, source_id)
    persisted_connector = await db_session.get(Connector, connector_id)
    assert result.reauth_required is False
    assert persisted_source is not None
    assert persisted_source.status is DriveSourceStatus.ACTIVE
    assert persisted_connector is not None
    assert persisted_connector.status is ConnectorStatus.ACTIVE
    assert persisted_connector.secret_id == replacement_secret_id


@pytest.mark.asyncio
async def test_cleanup_fences_embedding_task_already_waiting_on_provider() -> None:
    async with async_sessionmaker() as setup_session:
        source, document, versions, job = await _source_with_document(
            setup_session,
            version_states=(DocumentVersionState.PROCESSING,),
        )
        source_id = source.id
        document_id = document.id
        version_id = versions[0].id
        job_id = job.id
        organization_id = source.organization_id

    provider = BlockingEmbeddingProvider()
    try:
        async with async_sessionmaker() as publish_session:
            claimed_job = await publish_session.get(JobIntent, job_id)
            assert claimed_job is not None
            ingestion = DocumentIngestionService(
                publish_session,
                worker_id="old-worker",
            )
            publishing = asyncio.create_task(
                EmbeddingPublicationService(publish_session, provider).publish(
                    version_id,
                    before_publish=lambda: ingestion._assert_active_lease(claimed_job),
                )
            )
            await asyncio.wait_for(provider.started.wait(), timeout=5)

            async with async_sessionmaker() as cleanup_session:
                boundary = AssessmentBoundary(
                    [_file("drive-file", removed=True)],
                    descendants={"folder-a", "folder-b"},
                    current_files={
                        "root": _file(
                            "root",
                            mime_type="application/vnd.google-apps.folder",
                            parents=(),
                        )
                    },
                )
                await DriveSyncService(
                    cleanup_session, page_gateway=boundary
                ).sync(source_id, "cursor-1")

            provider.release.set()
            with pytest.raises(JobLeaseLost):
                await publishing
            await publish_session.rollback()

        async with async_sessionmaker() as verification_session:
            persisted_document = await verification_session.get(Document, document_id)
            persisted_version = await verification_session.get(
                DocumentVersion, version_id
            )
            persisted_job = await verification_session.get(JobIntent, job_id)
            assert persisted_document is not None
            assert persisted_document.current_version_id is None
            assert persisted_version is not None
            assert persisted_version.state is DocumentVersionState.REVOKED
            assert persisted_job is not None
            assert persisted_job.state is JobState.FAILED
            assert await verification_session.scalar(
                select(func.count(DocumentChunk.id)).where(
                    DocumentChunk.document_version_id == version_id
                )
            ) == 0
    finally:
        async with async_sessionmaker() as cleanup_session:
            organization = await cleanup_session.get(Organization, organization_id)
            if organization is not None:
                await cleanup_session.delete(organization)
                await cleanup_session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_inline_and_historical_cleanup_share_a_deadlock_free_lock_order() -> None:
    async with async_sessionmaker() as setup_session:
        source, document, versions, _ = await _source_with_document(
            setup_session,
            version_states=(DocumentVersionState.REVOKED,),
        )
        event = OutboxEvent(
            event_type="knowledge.document.cleanup.requested",
            aggregate_type="document",
            aggregate_id=document.id,
            payload={
                "organization_id": str(source.organization_id),
                "source_id": str(source.id),
                "document_id": str(document.id),
                "version_ids": [str(versions[0].id)],
            },
        )
        setup_session.add(event)
        await setup_session.commit()
        source_id = source.id
        document_id = document.id
        version_id = versions[0].id
        event_id = event.event_id
        organization_id = source.organization_id

    boundary = AssessmentBoundary(
        [_file("drive-file", removed=True)],
        descendants={"folder-a", "folder-b"},
        current_files={
            "root": _file(
                "root",
                mime_type="application/vnd.google-apps.folder",
                parents=(),
            )
        },
    )
    try:
        async with (
            async_sessionmaker() as inline_session,
            async_sessionmaker() as historical_session,
        ):
            await asyncio.wait_for(
                asyncio.gather(
                    DriveSyncService(
                        inline_session, page_gateway=boundary
                    ).sync(source_id, "cursor-1"),
                    _consume_document_cleanup_event(
                        event_id, db_session=historical_session
                    ),
                ),
                timeout=10,
            )

        async with async_sessionmaker() as verification_session:
            persisted_document = await verification_session.get(Document, document_id)
            persisted_version = await verification_session.get(
                DocumentVersion, version_id
            )
            assert persisted_document is not None
            assert persisted_document.current_version_id is None
            assert persisted_version is not None
            assert persisted_version.state is DocumentVersionState.REVOKED
            assert await verification_session.scalar(
                select(func.count(DocumentChunk.id)).where(
                    DocumentChunk.document_version_id == version_id
                )
            ) == 0
    finally:
        async with async_sessionmaker() as cleanup_session:
            organization = await cleanup_session.get(Organization, organization_id)
            if organization is not None:
                await cleanup_session.delete(organization)
                await cleanup_session.commit()
        await engine.dispose()
