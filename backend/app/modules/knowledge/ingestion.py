from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService
from app.modules.knowledge.chunking import DeterministicChunker
from app.modules.knowledge.drive_gateway import DriveFile
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
)
from app.modules.knowledge.parsers import DocumentParseError, DocumentParser, PdfParser, WordParser
from app.modules.knowledge.service import KnowledgeSourceService


class DocumentIngestionService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        chunker: DeterministicChunker | None = None,
        knowledge_source_service: KnowledgeSourceService | None = None,
        worker_id: str | None = None,
        job_lease_seconds: int = 300,
        job_lease_service: JobLeaseService | None = None,
    ) -> None:
        self._db_session = db_session
        self._chunker = chunker or DeterministicChunker()
        self._knowledge_source_service = knowledge_source_service
        self._worker_id = worker_id
        self._job_lease_seconds = job_lease_seconds
        self._job_lease_service = job_lease_service

    async def parse(self, job_id: UUID) -> DocumentVersion:
        """Parse one durable job through the existing authorized Drive download boundary."""
        if self._knowledge_source_service is None:
            raise RuntimeError("an authorized knowledge source service is required")
        if self._worker_id is None:
            raise RuntimeError("a unique document parse worker identity is required")
        lease_service = self._job_lease_service or JobLeaseService(self._db_session)
        job = await lease_service.claim(job_id, self._worker_id, self._job_lease_seconds)
        if job is None:
            return await self._completed_job_version_or_raise(job_id)
        if job.kind != "knowledge.document.parse":
            await self._fail_terminal(lease_service, job, "INVALID_DOCUMENT_PARSE_JOB")
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        try:
            recovered_version = await self._persisted_processing_version(job)
            if recovered_version is not None:
                await lease_service.complete(job.id, self._worker_id)
                return recovered_version
            document_id = self._document_id_from_payload(job.payload)
            document = await self._db_session.get(Document, document_id)
            if document is None:
                raise DocumentParseError("DOCUMENT_NOT_FOUND")
            source = await self._db_session.get(DriveSource, document.source_id)
            if source is None or source.organization_id != document.organization_id:
                raise DocumentParseError("DOCUMENT_SOURCE_NOT_FOUND")
            drive_file = self._drive_file_from_payload(job.payload)
            if drive_file.id != document.external_id:
                raise DocumentParseError("DOCUMENT_FILE_MISMATCH")
            content = await self._knowledge_source_service.download_authorized(
                self._db_session,
                source=source,
                file=drive_file,
            )
            version = await self.parse_bytes(document, content, drive_file.mime_type)
            job.payload = {**job.payload, "document_version_id": str(version.id)}
            await self._db_session.flush()
            await lease_service.complete(job.id, self._worker_id)
            return version
        except JobLeaseLost:
            raise
        except (DocumentParseError, HTTPException) as exc:
            error_code = (
                exc.code
                if isinstance(exc, DocumentParseError)
                else "DOCUMENT_DOWNLOAD_FORBIDDEN"
            )
            await self._fail_terminal(lease_service, job, error_code)
            raise
        except Exception:
            await lease_service.retry(
                job.id,
                self._worker_id,
                error_code="DOCUMENT_PARSE_TRANSIENT_FAILURE",
                error_class=ErrorClass.RETRYABLE,
            )
            raise

    async def parse_bytes(
        self,
        document: Document,
        content: bytes,
        mime_type: str,
        parser: DocumentParser | None = None,
    ) -> DocumentVersion:
        return await self.ingest_bytes(document, content, mime_type, parser)

    async def ingest_bytes(
        self,
        document: Document,
        content: bytes,
        mime_type: str,
        parser: DocumentParser | None = None,
    ) -> DocumentVersion:
        content_hash = sha256(content).hexdigest()
        version = DocumentVersion(
            document_id=document.id,
            state=DocumentVersionState.PROCESSING,
            content_sha256=content_hash,
        )
        self._db_session.add(version)
        await self._db_session.flush()
        selected_parser = parser or self._parser_for(mime_type)
        try:
            sections = selected_parser.parse(content)
            chunks = self._chunker.chunk(document_version_id=version.id, sections=sections)
            self._db_session.add_all(
                DocumentChunk(
                    id=chunk.id,
                    document_version_id=version.id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    page_number=chunk.page_number,
                    section=chunk.section,
                    token_count=chunk.token_count,
                    metadata_=chunk.metadata,
                )
                for chunk in chunks
            )
            await self._db_session.flush()
        except DocumentParseError as exc:
            version.state = DocumentVersionState.FAILED
            version.error_code = exc.code
            await self._db_session.flush()
            raise
        except Exception as exc:
            version.state = DocumentVersionState.FAILED
            version.error_code = "DOCUMENT_PARSE_FAILED"
            await self._db_session.flush()
            raise DocumentParseError() from exc
        return version

    @staticmethod
    def _parser_for(mime_type: str) -> DocumentParser:
        if mime_type == "application/pdf":
            return PdfParser()
        if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return WordParser()
        raise DocumentParseError("UNSUPPORTED_DOCUMENT_TYPE")

    @staticmethod
    def _document_id_from_payload(payload: Mapping[str, object]) -> UUID:
        raw_document_id = payload.get("document_id")
        if not isinstance(raw_document_id, str):
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        try:
            return UUID(raw_document_id)
        except ValueError as exc:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB") from exc

    @staticmethod
    def _drive_file_from_payload(payload: Mapping[str, object]) -> DriveFile:
        raw_file = payload.get("drive_file")
        if not isinstance(raw_file, Mapping):
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        file_id = raw_file.get("id")
        name = raw_file.get("name")
        mime_type = raw_file.get("mime_type")
        parent_ids = raw_file.get("parent_ids")
        if (
            not isinstance(file_id, str)
            or not isinstance(name, str)
            or not isinstance(mime_type, str)
            or not isinstance(parent_ids, list)
            or not all(isinstance(parent_id, str) for parent_id in parent_ids)
        ):
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        raw_modified_time = raw_file.get("modified_time")
        modified_time: datetime | None = None
        if raw_modified_time is not None:
            if not isinstance(raw_modified_time, str):
                raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
            try:
                modified_time = datetime.fromisoformat(raw_modified_time.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB") from exc
            if modified_time.tzinfo is None:
                modified_time = modified_time.replace(tzinfo=UTC)
        raw_link = raw_file.get("web_view_link")
        removed = raw_file.get("removed")
        if (raw_link is not None and not isinstance(raw_link, str)) or not isinstance(
            removed, bool
        ):
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        return DriveFile(
            id=file_id,
            name=name,
            mime_type=mime_type,
            modified_time=modified_time,
            parent_ids=tuple(parent_ids),
            web_view_link=raw_link,
            removed=removed,
        )

    async def _completed_job_version_or_raise(self, job_id: UUID) -> DocumentVersion:
        job = await self._db_session.get(JobIntent, job_id)
        if job is None or job.state is not JobState.SUCCEEDED:
            raise DocumentParseError("DOCUMENT_PARSE_JOB_UNAVAILABLE")
        version = await self._persisted_processing_version(job)
        if version is None:
            raise DocumentParseError("DOCUMENT_PARSE_JOB_UNAVAILABLE")
        return version

    async def _persisted_processing_version(self, job: JobIntent) -> DocumentVersion | None:
        raw_version_id = job.payload.get("document_version_id")
        if not isinstance(raw_version_id, str):
            return None
        try:
            version_id = UUID(raw_version_id)
        except ValueError as exc:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB") from exc
        version = await self._db_session.get(DocumentVersion, version_id)
        if version is None or version.state is not DocumentVersionState.PROCESSING:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        document_id = self._document_id_from_payload(job.payload)
        if version.document_id != document_id:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        return version

    async def _fail_terminal(
        self,
        lease_service: JobLeaseService,
        job: JobIntent,
        error_code: str,
    ) -> None:
        await lease_service.retry(
            job.id,
            self._worker_id or "",
            error_code=error_code,
            error_class=ErrorClass.NON_RETRYABLE,
        )
