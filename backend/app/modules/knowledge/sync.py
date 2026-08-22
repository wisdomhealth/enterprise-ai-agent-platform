"""Durable, page-at-a-time Google Drive synchronization.

The service deliberately owns no Google credentials.  It asks
``KnowledgeSourceService`` for a readonly, connector-backed change page, then
persists every effect of that page and its cursor in one database transaction.
"""

from dataclasses import dataclass
from datetime import UTC
from typing import cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.jobs.service import JobService
from app.modules.knowledge.drive_gateway import DriveFile, DriveGatewayFactory
from app.modules.knowledge.models import (
    Document,
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


def drive_sync_job_key(source_id: UUID | str, cursor: str | None) -> str:
    """The manual endpoint and periodic scheduler intentionally share this key."""
    return f"knowledge-drive-sync:{source_id}:{cursor or 'initial'}"


def revocation_state() -> DocumentVersionState:
    """Small public invariant used by the sync contract and its regression test."""
    return DocumentVersionState.REVOKED


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
        page_gateway: object | None = None,
    ) -> None:
        self._db_session = db_session
        self._knowledge_source_service = knowledge_source_service
        self._connector_service = connector_service
        self._drive_gateway_factory = drive_gateway_factory
        self._job_service = job_service or JobService()
        self._outbox_service = outbox_service or OutboxService()
        self._page_gateway = page_gateway

    async def fetch_page(self, source_id: UUID, cursor: str | None) -> DriveChangePage:
        """Fetch only.  Production callers must use :meth:`sync` to persist it."""
        if self._page_gateway is None:
            raise RuntimeError("a Drive change-page gateway is required")
        list_changes = getattr(self._page_gateway, "list_changes")
        files, next_cursor = await list_changes(source_id, cursor)
        return DriveChangePage(files=list(files), next_cursor=next_cursor)

    async def sync(self, source_id: UUID, page_token: str | None = None) -> SyncResult:
        if self._db_session is None:
            raise RuntimeError("a database session is required")
        source = await self._db_session.scalar(
            select(DriveSource).where(DriveSource.id == source_id).with_for_update()
        )
        if source is None:
            raise LookupError("knowledge source not found")
        if source.status is not DriveSourceStatus.ACTIVE:
            return SyncResult(source.id, source.sync_cursor, 0, 0, 0)

        cursor = page_token if page_token is not None else source.sync_cursor
        try:
            if cursor is None:
                cursor = await self._get_start_page_token(source)
                # A restart after an unavailable page must resume from a
                # durable Google-issued cursor, never repeat the empty-cursor
                # bootstrap request.
                source.sync_cursor = cursor
                await self._db_session.commit()
            files, next_cursor = await self._list_changes(source, cursor)
        except Exception as exc:
            if self._is_invalid_credential_error(exc):
                await self._mark_reauth_required(source)
                await self._db_session.commit()
                return SyncResult(source.id, source.sync_cursor, 0, 0, 0, reauth_required=True)
            source.status = DriveSourceStatus.ERROR
            await self._db_session.commit()
            raise

        enqueued = revoked = isolated = 0
        for drive_file in files:
            if drive_file.removed or not self._is_file_authorized(source, drive_file):
                did_revoke = await self._revoke_file(source, drive_file.id)
                revoked += int(did_revoke)
                isolated += int(not drive_file.removed)
                continue
            if drive_file.mime_type not in SUPPORTED_DOCUMENT_MIME_TYPES:
                continue
            document = await self._upsert_document(source, drive_file)
            await self._enqueue_parse(source, document, drive_file)
            enqueued += 1

        # This assignment and all page effects are committed together below.  A
        # failed insert/outbox enqueue therefore cannot skip a Drive change.
        source.sync_cursor = next_cursor
        source.status = DriveSourceStatus.ACTIVE
        await self._db_session.commit()
        return SyncResult(source.id, next_cursor, enqueued, revoked, isolated)

    async def _list_changes(
        self, source: DriveSource, sync_cursor: str | None
    ) -> tuple[list[DriveFile], str | None]:
        assert self._db_session is not None
        if self._page_gateway is not None:
            list_changes = getattr(self._page_gateway, "list_changes")
            page = await list_changes(self._db_session, source=source, sync_cursor=sync_cursor)
            return cast(tuple[list[DriveFile], str | None], page)
        if self._connector_service is None or self._drive_gateway_factory is None:
            raise RuntimeError("an encrypted Drive connector and readonly gateway are required")
        connector = await self._db_session.scalar(
            select(Connector).where(
                Connector.organization_id == source.organization_id,
                Connector.kind == ConnectorKind.DRIVE,
                Connector.status == ConnectorStatus.ACTIVE,
            )
        )
        if connector is None:
            raise RuntimeError("an active Google Drive connector is required")
        refresh_token = await self._connector_service.load_refresh_token(
            self._db_session, connector
        )
        connection = await self._drive_gateway_factory.create(refresh_token=refresh_token)
        return await connection.gateway.list_changes(sync_cursor)

    async def _get_start_page_token(self, source: DriveSource) -> str:
        assert self._db_session is not None
        if self._page_gateway is not None:
            get_start_page_token = getattr(self._page_gateway, "get_start_page_token")
            token = await get_start_page_token(self._db_session, source=source)
            if not isinstance(token, str) or not token:
                raise RuntimeError("Drive change cursor bootstrap returned no token")
            return token
        if self._connector_service is None or self._drive_gateway_factory is None:
            raise RuntimeError("an encrypted Drive connector and readonly gateway are required")
        connector = await self._db_session.scalar(
            select(Connector).where(
                Connector.organization_id == source.organization_id,
                Connector.kind == ConnectorKind.DRIVE,
                Connector.status == ConnectorStatus.ACTIVE,
            )
        )
        if connector is None:
            raise RuntimeError("an active Google Drive connector is required")
        refresh_token = await self._connector_service.load_refresh_token(
            self._db_session, connector
        )
        connection = await self._drive_gateway_factory.create(refresh_token=refresh_token)
        return await connection.gateway.get_start_page_token()

    def _is_file_authorized(self, source: DriveSource, drive_file: DriveFile) -> bool:
        boundary = self._knowledge_source_service or KnowledgeSourceService
        return boundary.is_file_authorized(source, drive_file)

    async def _upsert_document(self, source: DriveSource, drive_file: DriveFile) -> Document:
        assert self._db_session is not None
        document = await self._db_session.scalar(
            select(Document).where(
                Document.source_id == source.id, Document.external_id == drive_file.id
            )
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
            await self._db_session.flush()
        return document

    async def _enqueue_parse(
        self, source: DriveSource, document: Document, drive_file: DriveFile
    ) -> None:
        assert self._db_session is not None
        modified = (
            drive_file.modified_time.astimezone(UTC).isoformat() if drive_file.modified_time else ""
        )
        key = f"knowledge-document-parse:{source.id}:{drive_file.id}:{modified}"
        payload: dict[str, object] = {
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
            },
        }
        job = await self._job_service.enqueue(
            self._db_session, "knowledge.document.parse", key, payload
        )
        await self._outbox_service.add(
            self._db_session,
            "knowledge.document.parse.requested",
            "job",
            job.id,
            {"source_id": str(source.id), "document_id": str(document.id)},
        )

    async def _revoke_file(self, source: DriveSource, file_id: str) -> bool:
        assert self._db_session is not None
        document = await self._db_session.scalar(
            select(Document).where(Document.source_id == source.id, Document.external_id == file_id)
        )
        if document is None:
            return False
        revoked_ids = list(
            (
                await self._db_session.scalars(
                    update(DocumentVersion)
                    .where(
                        DocumentVersion.document_id == document.id,
                        DocumentVersion.state.not_in(
                            (DocumentVersionState.REVOKED, DocumentVersionState.DELETED)
                        ),
                    )
                    .values(state=DocumentVersionState.REVOKED)
                    .returning(DocumentVersion.id)
                )
            ).all()
        )
        document.current_version_id = None
        if not revoked_ids:
            return False
        await self._outbox_service.add(
            self._db_session,
            "knowledge.document.cleanup.requested",
            "document",
            document.id,
            {"source_id": str(source.id), "document_id": str(document.id)},
        )
        return True

    async def _mark_reauth_required(self, source: DriveSource) -> None:
        assert self._db_session is not None
        source.status = DriveSourceStatus.ERROR
        connector = await self._db_session.scalar(
            select(Connector).where(
                Connector.organization_id == source.organization_id,
                Connector.kind == ConnectorKind.DRIVE,
            )
        )
        if connector is not None:
            connector.status = ConnectorStatus.REAUTH_REQUIRED
            await self._outbox_service.add(
                self._db_session,
                "connector.reauthorization_required",
                "connector",
                connector.id,
                {"organization_id": str(source.organization_id), "kind": ConnectorKind.DRIVE.value},
            )

    @staticmethod
    def _is_invalid_credential_error(error: Exception) -> bool:
        status_code = getattr(error, "status_code", None)
        if status_code in (401, 403):
            return True
        return error.__class__.__name__ in {"RefreshError", "InvalidGrantError"}
