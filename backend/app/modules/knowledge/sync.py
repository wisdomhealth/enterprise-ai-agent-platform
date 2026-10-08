"""Two-phase Google Drive synchronization.

Drive is assessed without database locks. The result is then revalidated and
applied in one transaction with its cursor, cleanup, enqueue, and audit work.
"""

from dataclasses import dataclass
from datetime import UTC
from types import SimpleNamespace
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.connectors.encryption import EncryptedSecret
from app.modules.connectors.models import (
    Connector,
    ConnectorKind,
    ConnectorSecret,
    ConnectorStatus,
)
from app.modules.connectors.service import ConnectorService
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobService
from app.modules.knowledge.drive_gateway import (
    DriveFile,
    DriveFileUnavailable,
    DriveGatewayFactory,
    is_drive_authorization_error,
)
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    DriveSourceStatus,
)
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.outbox.service import OutboxService

SUPPORTED_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


@dataclass(frozen=True, slots=True)
class DriveChangePage:
    files: list[DriveFile]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SyncResult:
    source_id: UUID
    cursor: str | None
    enqueued_documents: int
    revoked_documents: int
    isolated_files: int
    reauth_required: bool = False
    parse_outbox_event_ids: tuple[UUID, ...] = ()
    # Compatibility only: new syncs delete chunks inline and create no event.
    cleanup_outbox_event_ids: tuple[UUID, ...] = ()
    root_unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    id: UUID
    organization_id: UUID
    knowledge_base_id: UUID
    root_folder_id: str
    include_descendants: bool
    allowed_descendant_ids: tuple[str, ...]
    sync_cursor: str | None
    status: DriveSourceStatus
    connection_identity: str
    documents: tuple[tuple[UUID, str], ...]
    connector_id: UUID | None = None
    connector_secret_id: UUID | None = None

    def boundary_source(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=self.id,
            organization_id=self.organization_id,
            knowledge_base_id=self.knowledge_base_id,
            root_folder_id=self.root_folder_id,
            include_descendants=self.include_descendants,
            allowed_descendant_ids=list(self.allowed_descendant_ids),
            sync_cursor=self.sync_cursor,
            status=self.status,
            connection_identity=self.connection_identity,
        )


@dataclass(frozen=True, slots=True)
class _Assessment:
    next_cursor: str
    descendant_ids: tuple[str, ...]
    files_to_ingest: tuple[DriveFile, ...]
    revocations: tuple[tuple[UUID, str], ...]
    isolated_files: int
    root_unavailable_reason: str | None = None


class StaleDriveAssessment(RuntimeError):
    """The source or authorization changed while Drive was assessed."""


def drive_sync_job_key(source_id: UUID | str, cursor: str | None) -> str:
    return f"knowledge-drive-sync:{source_id}:{cursor or 'initial'}"


def revocation_state() -> DocumentVersionState:
    return DocumentVersionState.REVOKED


def knowledge_worker_actor_id(organization_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"knowledge-worker:{organization_id}")


class DriveSyncService:
    def __init__(
        self,
        db_session: AsyncSession | None = None,
        *,
        knowledge_source_service: KnowledgeSourceService | None = None,
        connector_service: ConnectorService | None = None,
        drive_gateway_factory: DriveGatewayFactory | None = None,
        job_service: JobService | None = None,
        outbox_service: OutboxService | None = None,
        audit_service: AuditService | None = None,
        page_gateway: object | None = None,
    ) -> None:
        self._db_session = db_session
        self._knowledge_source_service = knowledge_source_service
        self._connector_service = connector_service
        self._drive_gateway_factory = drive_gateway_factory
        self._job_service = job_service or JobService()
        self._outbox_service = outbox_service or OutboxService()
        self._audit_service = audit_service or AuditService()
        self._page_gateway = page_gateway

    async def fetch_page(self, source_id: UUID, cursor: str | None) -> DriveChangePage:
        if self._page_gateway is None:
            raise RuntimeError("a Drive change-page gateway is required")
        files, next_cursor = await getattr(self._page_gateway, "list_changes")(
            source_id, cursor
        )
        return DriveChangePage(files=list(files), next_cursor=next_cursor)

    async def sync(
        self,
        source_id: UUID,
        page_token: str | None = None,
        *,
        parent_sync_job_id: UUID | None = None,
    ) -> SyncResult:
        if self._db_session is None:
            raise RuntimeError("a database session is required")
        snapshot, encrypted_secret = await self._read_snapshot(source_id)
        if snapshot.status is not DriveSourceStatus.ACTIVE:
            return SyncResult(snapshot.id, snapshot.sync_cursor, 0, 0, 0)
        if page_token is not None and page_token != snapshot.sync_cursor:
            await self._db_session.rollback()
            raise StaleDriveAssessment("Drive cursor changed before assessment")
        cursor = page_token if page_token is not None else snapshot.sync_cursor

        # Explicitly close the read transaction before any Drive call.
        await self._db_session.rollback()
        try:
            gateway, connection_identity = await self._open_gateway(
                snapshot, encrypted_secret
            )
            if connection_identity != snapshot.connection_identity:
                raise StaleDriveAssessment(
                    "Drive authorization identity changed during assessment"
                )
            if cursor is None:
                cursor = await self._get_start_page_token(snapshot, gateway)
            files, next_cursor = await self._list_changes(snapshot, gateway, cursor)
            if not isinstance(next_cursor, str) or not next_cursor:
                raise RuntimeError("Drive change listing did not return a continuation cursor")
            assessment = await self._assess(snapshot, gateway, files, next_cursor)
        except Exception as error:
            await self._db_session.rollback()
            if is_drive_authorization_error(error):
                changed = await self._mark_reauth_required(snapshot)
                return SyncResult(
                    snapshot.id,
                    snapshot.sync_cursor,
                    0,
                    0,
                    0,
                    reauth_required=changed,
                )
            raise

        return await self._apply_assessment(
            snapshot,
            assessment,
            parent_sync_job_id=parent_sync_job_id,
        )

    async def _read_snapshot(
        self, source_id: UUID
    ) -> tuple[_SourceSnapshot, EncryptedSecret | None]:
        assert self._db_session is not None
        source = await self._db_session.get(DriveSource, source_id)
        if source is None:
            raise LookupError("knowledge source not found")
        documents = tuple(
            (
                await self._db_session.execute(
                    select(Document.id, Document.external_id)
                    .where(
                        Document.organization_id == source.organization_id,
                        Document.source_id == source.id,
                    )
                    .order_by(Document.id)
                )
            ).all()
        )
        connector = await self._db_session.scalar(
            select(Connector).where(
                Connector.organization_id == source.organization_id,
                Connector.kind == ConnectorKind.DRIVE,
            )
        )
        encrypted_secret: EncryptedSecret | None = None
        if self._page_gateway is None:
            if self._connector_service is None or self._drive_gateway_factory is None:
                raise RuntimeError(
                    "an encrypted Drive connector and readonly gateway are required"
                )
            if connector is None or connector.status is not ConnectorStatus.ACTIVE:
                raise RuntimeError("an active Google Drive connector is required")
            secret = await self._db_session.get(ConnectorSecret, connector.secret_id)
            if secret is None or secret.organization_id != connector.organization_id:
                raise LookupError("connector secret is unavailable")
            encrypted_secret = EncryptedSecret(
                ciphertext=secret.ciphertext,
                encrypted_data_key=secret.encrypted_data_key,
                nonce=secret.nonce,
                algorithm=secret.algorithm,
                key_version=secret.key_version,
            )
        snapshot = _SourceSnapshot(
            id=source.id,
            organization_id=source.organization_id,
            knowledge_base_id=source.knowledge_base_id,
            root_folder_id=source.root_folder_id,
            include_descendants=source.include_descendants,
            allowed_descendant_ids=tuple(sorted(source.allowed_descendant_ids)),
            sync_cursor=source.sync_cursor,
            status=source.status,
            connection_identity=source.connection_identity,
            documents=cast(tuple[tuple[UUID, str], ...], documents),
            connector_id=connector.id if connector is not None else None,
            connector_secret_id=connector.secret_id if connector is not None else None,
        )
        return snapshot, encrypted_secret

    async def _open_gateway(
        self, snapshot: _SourceSnapshot, encrypted_secret: EncryptedSecret | None
    ) -> tuple[object, str]:
        if self._page_gateway is not None:
            return self._page_gateway, snapshot.connection_identity
        assert self._drive_gateway_factory is not None
        assert self._connector_service is not None
        assert encrypted_secret is not None
        refresh_token = await self._connector_service.decrypt_refresh_token(
            encrypted_secret
        )
        connection = await self._drive_gateway_factory.create(
            refresh_token=refresh_token
        )
        return connection.gateway, connection.connection_identity

    async def _get_start_page_token(
        self, snapshot: _SourceSnapshot, gateway: object
    ) -> str:
        method = getattr(gateway, "get_start_page_token")
        if self._page_gateway is not None:
            token = await method(None, source=snapshot.boundary_source())
        else:
            token = await method()
        if not isinstance(token, str) or not token:
            raise RuntimeError("Drive change cursor bootstrap returned no token")
        return token

    async def _list_changes(
        self, snapshot: _SourceSnapshot, gateway: object, cursor: str
    ) -> tuple[list[DriveFile], str | None]:
        method = getattr(gateway, "list_changes")
        if self._page_gateway is not None:
            result = await method(
                None, source=snapshot.boundary_source(), sync_cursor=cursor
            )
        else:
            result = await method(cursor)
        return cast(tuple[list[DriveFile], str | None], result)

    async def _assess(
        self,
        snapshot: _SourceSnapshot,
        gateway: object,
        files: list[DriveFile],
        next_cursor: str,
    ) -> _Assessment:
        documents_by_external_id = {
            external_id: document_id for document_id, external_id in snapshot.documents
        }
        root_change = next((item for item in files if item.id == snapshot.root_folder_id), None)
        if root_change is not None and (root_change.removed or root_change.trashed):
            reason = "TRASHED" if root_change.trashed else "REMOVED_OR_ACCESS_LOST"
            return self._root_unavailable(snapshot, next_cursor, reason)

        get_file = getattr(gateway, "get", None)
        resolve_descendants = getattr(gateway, "resolve_descendant_folder_ids", None)
        descendant_ids = set(snapshot.allowed_descendant_ids)
        if get_file is not None:
            try:
                root = await get_file(snapshot.root_folder_id)
            except DriveFileUnavailable as error:
                return self._root_unavailable(snapshot, next_cursor, error.reason.value)
            if root is None or root.trashed:
                reason = (
                    "TRASHED"
                    if root is not None and root.trashed
                    else "NOT_FOUND_OR_NO_ACCESS"
                )
                return self._root_unavailable(snapshot, next_cursor, reason)
        if snapshot.include_descendants and resolve_descendants is not None:
            descendant_ids = set(await resolve_descendants(snapshot.root_folder_id))
        elif not snapshot.include_descendants:
            descendant_ids = set()

        authorized_parents = descendant_ids | {snapshot.root_folder_id}
        revocations: dict[UUID, str] = {}
        ingest_by_id: dict[str, DriveFile] = {}
        isolated = 0
        folder_uncertainty = descendant_ids != set(snapshot.allowed_descendant_ids)
        for item in files:
            if item.id == snapshot.root_folder_id:
                continue
            document_id = documents_by_external_id.get(item.id)
            if item.removed:
                if document_id is not None:
                    revocations[document_id] = "REMOVED_OR_ACCESS_LOST"
                else:
                    folder_uncertainty = True
                continue
            if item.trashed:
                if document_id is not None:
                    revocations[document_id] = "TRASHED"
                else:
                    folder_uncertainty = True
                continue
            if item.mime_type == DRIVE_FOLDER_MIME_TYPE:
                folder_uncertainty = True
                continue
            if item.mime_type not in SUPPORTED_DOCUMENT_MIME_TYPES:
                continue
            if set(item.parent_ids).intersection(authorized_parents):
                ingest_by_id[item.id] = item
            elif document_id is not None:
                revocations[document_id] = (
                    "SCOPE_UNVERIFIABLE"
                    if not item.parent_ids
                    else "OUTSIDE_AUTHORIZED_ROOT"
                )
                isolated += 1

        # Removed changes have no MIME type, so an unknown removed ID may be a
        # folder. Recheck every imported document against the fresh folder set.
        if folder_uncertainty:
            if get_file is None:
                raise RuntimeError("Drive scope verification is unavailable")
            for document_id, external_id in snapshot.documents:
                if document_id in revocations:
                    continue
                try:
                    current = await get_file(external_id)
                except DriveFileUnavailable as error:
                    revocations[document_id] = error.reason.value
                    continue
                if current is None:
                    revocations[document_id] = "NOT_FOUND_OR_NO_ACCESS"
                elif current.trashed:
                    revocations[document_id] = "TRASHED"
                elif not current.parent_ids:
                    revocations[document_id] = "SCOPE_UNVERIFIABLE"
                elif not set(current.parent_ids).intersection(authorized_parents):
                    revocations[document_id] = "OUTSIDE_AUTHORIZED_ROOT"

        revoked_external_ids = {
            external_id
            for document_id, external_id in snapshot.documents
            if document_id in revocations
        }
        for external_id in revoked_external_ids:
            ingest_by_id.pop(external_id, None)
        return _Assessment(
            next_cursor=next_cursor,
            descendant_ids=tuple(sorted(descendant_ids)),
            files_to_ingest=tuple(ingest_by_id[key] for key in sorted(ingest_by_id)),
            revocations=tuple(sorted(revocations.items(), key=lambda item: str(item[0]))),
            isolated_files=isolated,
        )

    @staticmethod
    def _root_unavailable(
        snapshot: _SourceSnapshot, next_cursor: str, reason: str
    ) -> _Assessment:
        return _Assessment(
            next_cursor=next_cursor,
            descendant_ids=snapshot.allowed_descendant_ids,
            files_to_ingest=(),
            revocations=tuple(
                (document_id, "AUTHORIZED_ROOT_UNAVAILABLE")
                for document_id, _ in snapshot.documents
            ),
            isolated_files=0,
            root_unavailable_reason=reason,
        )

    async def _apply_assessment(
        self,
        snapshot: _SourceSnapshot,
        assessment: _Assessment,
        *,
        parent_sync_job_id: UUID | None,
    ) -> SyncResult:
        assert self._db_session is not None
        parse_event_ids: list[UUID] = []
        revoked_count = 0
        async with self._db_session.begin():
            source = await self._db_session.scalar(
                select(DriveSource).where(DriveSource.id == snapshot.id).with_for_update()
            )
            if source is None or not self._source_matches_snapshot(source, snapshot):
                raise StaleDriveAssessment("Drive source changed during assessment")
            await self._revalidate_connector(snapshot)

            revocation_map = dict(assessment.revocations)
            affected_ids = sorted(revocation_map, key=str)
            existing_documents_by_external_id = {
                external_id: document_id
                for document_id, external_id in snapshot.documents
            }
            ingest_keys_by_document = {
                existing_documents_by_external_id[drive_file.id]: self._parse_job_key(
                    source.id, drive_file
                )
                for drive_file in assessment.files_to_ingest
                if drive_file.id in existing_documents_by_external_id
            }
            ingest_job_keys = [
                self._parse_job_key(source.id, drive_file)
                for drive_file in assessment.files_to_ingest
            ]
            locked_document_ids = sorted(
                set(affected_ids) | set(ingest_keys_by_document), key=str
            )
            parse_jobs = await self._lock_parse_jobs(
                source.id, locked_document_ids, ingest_job_keys
            )
            affected_values = {str(value) for value in affected_ids}
            ingest_keys_by_document_value = {
                str(document_id): key
                for document_id, key in ingest_keys_by_document.items()
            }
            for job in parse_jobs:
                if job.state not in (
                    JobState.PENDING,
                    JobState.RUNNING,
                    JobState.RECONCILIATION,
                ):
                    continue
                raw_document_id = job.payload.get("document_id")
                if not isinstance(raw_document_id, str):
                    continue
                if raw_document_id in affected_values:
                    self._invalidate_parse_job(job, "DOCUMENT_REVOKED")
                    continue
                current_key = ingest_keys_by_document_value.get(raw_document_id)
                if current_key is not None and job.idempotency_key != current_key:
                    self._invalidate_parse_job(job, "DOCUMENT_SUPERSEDED")
            if affected_ids:
                documents = await self._lock_cleanup_documents(source, affected_ids)
                versions = await self._lock_cleanup_versions(affected_ids)
                versions_by_document: dict[UUID, list[DocumentVersion]] = {
                    value: [] for value in affected_ids
                }
                for version in versions:
                    versions_by_document[version.document_id].append(version)

                for document in documents:
                    document_versions = versions_by_document[document.id]
                    version_ids = [version.id for version in document_versions]
                    deleted_count = 0
                    if version_ids:
                        deleted_count = len(
                            (
                                await self._db_session.scalars(
                                    delete(DocumentChunk)
                                    .where(
                                        DocumentChunk.document_version_id.in_(version_ids)
                                    )
                                    .returning(DocumentChunk.id)
                                )
                            ).all()
                        )
                    changed = document.current_version_id is not None
                    document.current_version_id = None
                    for version in document_versions:
                        changed = changed or version.state is not DocumentVersionState.REVOKED
                        version.state = DocumentVersionState.REVOKED
                    if changed or deleted_count:
                        revoked_count += 1
                        await self._record_cleanup_audit(
                            source,
                            document,
                            document_versions,
                            reason=revocation_map[document.id],
                            deleted_chunk_count=deleted_count,
                            sync_job_id=parent_sync_job_id,
                            root_unavailable_reason=assessment.root_unavailable_reason,
                        )

            for drive_file in assessment.files_to_ingest:
                document = await self._upsert_document(source, drive_file)
                parse_event_ids.append(
                    await self._enqueue_parse(
                        source,
                        document,
                        drive_file,
                        parent_sync_job_id=parent_sync_job_id,
                    )
                )

            source.allowed_descendant_ids = list(assessment.descendant_ids)
            source.sync_cursor = assessment.next_cursor
            if assessment.root_unavailable_reason is not None:
                source.status = DriveSourceStatus.DISABLED
                await self._audit_service.record_actor(
                    self._db_session,
                    organization_id=source.organization_id,
                    actor_id=knowledge_worker_actor_id(source.organization_id),
                    action="knowledge.drive_source.root_unavailable",
                    object_type="drive_source",
                    object_id=source.id,
                    outcome="DISABLED",
                    details={
                        "reason": assessment.root_unavailable_reason,
                        "sync_job_id": (
                            str(parent_sync_job_id)
                            if parent_sync_job_id is not None
                            else None
                        ),
                    },
                    safe_detail_keys=("reason", "sync_job_id"),
                )

        return SyncResult(
            source_id=snapshot.id,
            cursor=assessment.next_cursor,
            enqueued_documents=len(assessment.files_to_ingest),
            revoked_documents=revoked_count,
            isolated_files=assessment.isolated_files,
            parse_outbox_event_ids=tuple(parse_event_ids),
            root_unavailable_reason=assessment.root_unavailable_reason,
        )

    async def _revalidate_connector(self, snapshot: _SourceSnapshot) -> None:
        if snapshot.connector_id is None:
            return
        assert self._db_session is not None
        connector = await self._db_session.scalar(
            select(Connector)
            .where(Connector.id == snapshot.connector_id)
            .with_for_update()
        )
        if (
            connector is None
            or connector.organization_id != snapshot.organization_id
            or connector.kind is not ConnectorKind.DRIVE
            or connector.status is not ConnectorStatus.ACTIVE
            or connector.secret_id != snapshot.connector_secret_id
        ):
            raise StaleDriveAssessment("Drive authorization changed during assessment")

    async def _lock_parse_jobs(
        self,
        source_id: UUID,
        document_ids: list[UUID],
        idempotency_keys: list[str],
    ) -> list[JobIntent]:
        assert self._db_session is not None
        if not document_ids and not idempotency_keys:
            return []
        predicates = []
        if document_ids:
            predicates.append(
                JobIntent.payload["document_id"].as_string().in_(
                    [str(value) for value in document_ids]
                )
            )
        if idempotency_keys:
            predicates.append(JobIntent.idempotency_key.in_(idempotency_keys))
        return list(
            (
                await self._db_session.scalars(
                    select(JobIntent)
                    .where(
                        JobIntent.kind == "knowledge.document.parse",
                        JobIntent.payload["source_id"].as_string() == str(source_id),
                        or_(*predicates),
                    )
                    .order_by(JobIntent.id)
                    .with_for_update()
                )
            ).all()
        )

    @staticmethod
    def _invalidate_parse_job(job: JobIntent, error_code: str) -> None:
        job.state = JobState.FAILED
        job.lease_owner = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.last_error_code = error_code
        job.error_class = ErrorClass.NON_RETRYABLE
        job.version += 1
        job.updated_at = func.clock_timestamp()

    async def _lock_cleanup_documents(
        self, source: DriveSource, document_ids: list[UUID]
    ) -> list[Document]:
        assert self._db_session is not None
        documents = list(
            (
                await self._db_session.scalars(
                    select(Document)
                    .where(
                        Document.id.in_(document_ids),
                        Document.organization_id == source.organization_id,
                        Document.knowledge_base_id == source.knowledge_base_id,
                        Document.source_id == source.id,
                    )
                    .order_by(Document.id)
                    .with_for_update()
                )
            ).all()
        )
        if {item.id for item in documents} != set(document_ids):
            raise StaleDriveAssessment("a cleanup document changed ownership")
        return documents

    async def _lock_cleanup_versions(
        self, document_ids: list[UUID]
    ) -> list[DocumentVersion]:
        assert self._db_session is not None
        return list(
            (
                await self._db_session.scalars(
                    select(DocumentVersion)
                    .where(
                        DocumentVersion.document_id.in_(document_ids),
                        DocumentVersion.state != DocumentVersionState.DELETED,
                    )
                    .order_by(DocumentVersion.id)
                    .with_for_update()
                )
            ).all()
        )

    async def _record_cleanup_audit(
        self,
        source: DriveSource,
        document: Document,
        versions: list[DocumentVersion],
        *,
        reason: str,
        deleted_chunk_count: int,
        sync_job_id: UUID | None,
        root_unavailable_reason: str | None,
    ) -> None:
        assert self._db_session is not None
        await self._audit_service.record_actor(
            self._db_session,
            organization_id=source.organization_id,
            actor_id=knowledge_worker_actor_id(source.organization_id),
            action="knowledge.document.revocation_cleanup.completed",
            object_type="document",
            object_id=document.id,
            outcome="SUCCESS",
            details={
                "source_id": str(source.id),
                "reason": reason,
                "version_ids": sorted(str(version.id) for version in versions),
                "deleted_chunk_count": deleted_chunk_count,
                "sync_job_id": str(sync_job_id) if sync_job_id is not None else None,
                "root_unavailable_reason": root_unavailable_reason,
            },
            safe_detail_keys=(
                "source_id",
                "reason",
                "version_ids",
                "deleted_chunk_count",
                "sync_job_id",
                "root_unavailable_reason",
            ),
        )

    @staticmethod
    def _source_matches_snapshot(source: DriveSource, snapshot: _SourceSnapshot) -> bool:
        return (
            source.organization_id == snapshot.organization_id
            and source.knowledge_base_id == snapshot.knowledge_base_id
            and source.status is snapshot.status
            and source.root_folder_id == snapshot.root_folder_id
            and source.include_descendants is snapshot.include_descendants
            and tuple(sorted(source.allowed_descendant_ids))
            == snapshot.allowed_descendant_ids
            and source.sync_cursor == snapshot.sync_cursor
            and source.connection_identity == snapshot.connection_identity
        )

    async def _upsert_document(self, source: DriveSource, drive_file: DriveFile) -> Document:
        assert self._db_session is not None
        document = await self._db_session.scalar(
            select(Document)
            .where(
                Document.organization_id == source.organization_id,
                Document.source_id == source.id,
                Document.external_id == drive_file.id,
            )
            .with_for_update()
        )
        if document is None:
            document = Document(
                organization_id=source.organization_id,
                knowledge_base_id=source.knowledge_base_id,
                source_id=source.id,
                external_id=drive_file.id,
                title=drive_file.name,
                mime_type=drive_file.mime_type,
            )
            self._db_session.add(document)
            await self._db_session.flush()
        else:
            document.title = drive_file.name
            document.mime_type = drive_file.mime_type
        return document

    async def _enqueue_parse(
        self,
        source: DriveSource,
        document: Document,
        drive_file: DriveFile,
        *,
        parent_sync_job_id: UUID | None,
    ) -> UUID:
        assert self._db_session is not None
        modified = self._drive_modified_value(drive_file)
        key = self._parse_job_key(source.id, drive_file)
        job = await self._job_service.enqueue(
            self._db_session,
            "knowledge.document.parse",
            key,
            {
                "source_id": str(source.id),
                "document_id": str(document.id),
                "drive_file": {
                    "id": drive_file.id,
                    "name": drive_file.name,
                    "mime_type": drive_file.mime_type,
                    "modified_time": modified or None,
                    "parent_ids": list(drive_file.parent_ids),
                    "web_view_link": drive_file.web_view_link,
                    "removed": False,
                    "trashed": False,
                },
            },
        )
        event = await self._outbox_service.add(
            self._db_session,
            "knowledge.document.parse.requested",
            "job",
            job.id,
            {
                "organization_id": str(source.organization_id),
                "source_id": str(source.id),
                "document_id": str(document.id),
                **(
                    {"parent_sync_job_id": str(parent_sync_job_id)}
                    if parent_sync_job_id is not None
                    else {}
                ),
            },
        )
        return event.event_id

    @staticmethod
    def _drive_modified_value(drive_file: DriveFile) -> str:
        return (
            drive_file.modified_time.astimezone(UTC).isoformat()
            if drive_file.modified_time
            else ""
        )

    @classmethod
    def _parse_job_key(cls, source_id: UUID, drive_file: DriveFile) -> str:
        return (
            f"knowledge-document-parse:{source_id}:{drive_file.id}:"
            f"{cls._drive_modified_value(drive_file)}"
        )

    async def _mark_reauth_required(self, snapshot: _SourceSnapshot) -> bool:
        assert self._db_session is not None
        async with self._db_session.begin():
            source = await self._db_session.scalar(
                select(DriveSource).where(DriveSource.id == snapshot.id).with_for_update()
            )
            if source is None or not self._source_matches_snapshot(source, snapshot):
                return False
            connector = await self._db_session.scalar(
                select(Connector)
                .where(
                    Connector.organization_id == snapshot.organization_id,
                    Connector.kind == ConnectorKind.DRIVE,
                )
                .with_for_update()
            )
            if connector is None:
                return False
            if snapshot.connector_id is None or (
                connector.id != snapshot.connector_id
                or connector.secret_id != snapshot.connector_secret_id
                or connector.status is not ConnectorStatus.ACTIVE
            ):
                return False
            source.status = DriveSourceStatus.ERROR
            connector.status = ConnectorStatus.REAUTH_REQUIRED
            await self._outbox_service.add(
                self._db_session,
                "connector.reauthorization_required",
                "connector",
                connector.id,
                {
                    "organization_id": str(snapshot.organization_id),
                    "kind": ConnectorKind.DRIVE.value,
                },
            )
        return True
