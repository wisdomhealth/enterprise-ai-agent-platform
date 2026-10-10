from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.audit.service import AuditService
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService
from app.modules.knowledge.chunking import Chunk, DeterministicChunker
from app.modules.knowledge.drive_gateway import DriveFile
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    DriveSourceStatus,
)
from app.modules.knowledge.parsers import DocumentParseError, DocumentParser, PdfParser, WordParser
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.knowledge.sync import knowledge_worker_actor_id

if TYPE_CHECKING:
    from app.modules.rag.types import EmbeddingProvider


@dataclass(frozen=True, slots=True)
class _PreparedVersion:
    id: UUID
    content_sha256: str
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True, slots=True)
class _JobLeaseSnapshot:
    id: UUID
    version: int


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
        embedding_provider: "EmbeddingProvider | None" = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._db_session = db_session
        if chunker is None:
            settings = Settings()
            chunker = DeterministicChunker(
                chunk_size=settings.knowledge_chunk_size,
                chunk_overlap=settings.knowledge_chunk_overlap,
            )
        self._chunker = chunker
        self._knowledge_source_service = knowledge_source_service
        self._worker_id = worker_id
        self._job_lease_seconds = job_lease_seconds
        self._job_lease_service = job_lease_service
        self._embedding_provider = embedding_provider
        self._audit_service = audit_service or AuditService()

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
        claimed_job_id = job.id
        claimed_job_version = job.version
        if job.kind != "knowledge.document.parse":
            await self._fail_terminal(
                lease_service,
                claimed_job_id,
                claimed_job_version,
                "INVALID_DOCUMENT_PARSE_JOB",
            )
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        # Publish the claim before external I/O so an expired lease can be taken over
        # without waiting for this worker's parse transaction to end.
        await self._db_session.commit()
        try:
            version = await self._version_from_job(job)
            if version is not None and version.state is DocumentVersionState.RETRIEVABLE:
                await lease_service.complete(
                    job.id,
                    self._worker_id,
                    expected_version=claimed_job_version,
                )
                await self._db_session.commit()
                return version
            if version is not None and version.state is not DocumentVersionState.PROCESSING:
                raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
            if version is None:
                document_id = self._document_id_from_payload(job.payload)
                document = await self._db_session.get(Document, document_id)
                if document is None:
                    raise DocumentParseError("DOCUMENT_NOT_FOUND")
                source = await self._db_session.get(DriveSource, document.source_id)
                if source is None or source.organization_id != document.organization_id:
                    raise DocumentParseError("DOCUMENT_SOURCE_NOT_FOUND")
                source_id = source.id
                drive_file = self._drive_file_from_payload(job.payload)
                if drive_file.id != document.external_id:
                    raise DocumentParseError("DOCUMENT_FILE_MISMATCH")
                content = await self._knowledge_source_service.download_authorized(
                    self._db_session,
                    source=source,
                    file=drive_file,
                )
                # Drive download ends with no open database transaction. Check
                # the lease once before CPU-heavy parsing, parse without locks,
                # then fence again in the write checkpoint.
                await self._assert_active_lease_without_lock(
                    claimed_job_id, claimed_job_version
                )
                await self._db_session.rollback()
                content_hash = sha256(content).hexdigest()
                checkpoint_job = await self._db_session.get(
                    JobIntent, claimed_job_id, populate_existing=True
                )
                if checkpoint_job is None:
                    raise JobLeaseLost(claimed_job_id)
                document = await self._lock_parse_checkpoint(
                    checkpoint_job,
                    document_id,
                    source_id=source_id,
                    drive_file=drive_file,
                )
                matching, locked_versions = await self._lock_versions_and_find_match(
                    document.id, content_hash
                )
                if matching is not None:
                    version = await self._reuse_or_reject_matching_version(
                        lease_service,
                        checkpoint_job,
                        document,
                        matching,
                        locked_versions,
                        expected_job_version=claimed_job_version,
                    )
                    await self._db_session.commit()
                    return version
                await self._db_session.rollback()

                prepared = self._prepare_version(
                    content,
                    drive_file.mime_type,
                    content_sha256=content_hash,
                )
                checkpoint_job = await self._db_session.get(
                    JobIntent, claimed_job_id, populate_existing=True
                )
                if checkpoint_job is None:
                    raise JobLeaseLost(claimed_job_id)
                document = await self._lock_parse_checkpoint(
                    checkpoint_job,
                    document_id,
                    source_id=source_id,
                    drive_file=drive_file,
                )
                matching, locked_versions = await self._lock_versions_and_find_match(
                    document.id, prepared.content_sha256
                )
                if matching is not None:
                    version = await self._reuse_or_reject_matching_version(
                        lease_service,
                        checkpoint_job,
                        document,
                        matching,
                        locked_versions,
                        expected_job_version=claimed_job_version,
                    )
                    await self._db_session.commit()
                    return version
                version = await self._persist_prepared_version(document, prepared)
                await self._commit_processing_checkpoint(checkpoint_job, version)
                job = await self._db_session.get(
                    JobIntent, claimed_job_id, populate_existing=True
                )
                if job is None:
                    raise JobLeaseLost(claimed_job_id)
            version = await self._publish_processing_version(job, version)
            await lease_service.complete(
                job.id,
                self._worker_id,
                expected_version=claimed_job_version,
            )
            await self._db_session.commit()
            return version
        except JobLeaseLost:
            await self._db_session.rollback()
            raise
        except (DocumentParseError, HTTPException) as exc:
            error_code = (
                exc.code
                if isinstance(exc, DocumentParseError)
                else "DOCUMENT_DOWNLOAD_FORBIDDEN"
            )
            await self._fail_terminal(
                lease_service,
                claimed_job_id,
                claimed_job_version,
                error_code,
            )
            raise
        except Exception:
            # A failed embedding flush leaves SQLAlchemy rollback-required.  Restore
            # the session before applying the existing lease-fenced retry transition.
            await self._db_session.rollback()
            await lease_service.retry(
                claimed_job_id,
                self._worker_id,
                error_code="DOCUMENT_PARSE_TRANSIENT_FAILURE",
                error_class=ErrorClass.RETRYABLE,
                expected_version=claimed_job_version,
            )
            await self._db_session.commit()
            raise

    async def parse_bytes(
        self,
        document: Document,
        content: bytes,
        mime_type: str,
        parser: DocumentParser | None = None,
    ) -> DocumentVersion:
        return await self.ingest_bytes(document, content, mime_type, parser)

    async def publish_embeddings(
        self, version_id: UUID, provider: "EmbeddingProvider"
    ) -> DocumentVersion:
        """Publish a completed parse only through the atomic embedding boundary."""
        from app.modules.rag.embeddings import EmbeddingPublicationService

        return await EmbeddingPublicationService(self._db_session, provider).publish(version_id)

    async def _publish_processing_version(
        self,
        job: JobIntent,
        version: DocumentVersion,
    ) -> DocumentVersion:
        """Embed a durable checkpoint, retaining the Task 8 lease fence at publication."""
        from app.modules.rag.embeddings import EmbeddingPublicationService, OpenAIEmbeddingProvider

        provider = self._embedding_provider or OpenAIEmbeddingProvider.from_settings(Settings())
        version_id = version.id
        document_id = version.document_id
        job_snapshot = _JobLeaseSnapshot(id=job.id, version=job.version)
        source_id = await self._db_session.scalar(
            select(Document.source_id).where(Document.id == document_id)
        )
        if source_id is None:
            raise DocumentParseError("DOCUMENT_NOT_FOUND")
        drive_file = self._drive_file_from_payload(job.payload)
        await self._db_session.rollback()

        async def lock_before_publish() -> None:
            await self._lock_parse_checkpoint(
                job_snapshot,
                document_id,
                source_id=source_id,
                drive_file=drive_file,
            )

        return await EmbeddingPublicationService(self._db_session, provider).publish(
            version_id,
            before_publish=lock_before_publish,
            transition=lambda document, target, versions, chunks: (
                self._transition_current_version(
                    job_snapshot,
                    document,
                    target,
                    versions,
                    chunks,
                )
            ),
        )

    async def _assert_active_lease(
        self, job: JobIntent | _JobLeaseSnapshot
    ) -> None:
        lease_owner = await self._db_session.scalar(
            select(JobIntent.id)
            .where(
                JobIntent.id == job.id,
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_owner == self._worker_id,
                JobIntent.version == job.version,
                JobIntent.lease_expires_at.is_not(None),
                JobIntent.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update()
        )
        if lease_owner is None:
            raise JobLeaseLost(job.id)

    async def _assert_active_lease_without_lock(
        self, job_id: UUID, expected_version: int
    ) -> None:
        lease_owner = await self._db_session.scalar(
            select(JobIntent.id).where(
                JobIntent.id == job_id,
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_owner == self._worker_id,
                JobIntent.version == expected_version,
                JobIntent.lease_expires_at.is_not(None),
                JobIntent.lease_expires_at > func.clock_timestamp(),
            )
        )
        if lease_owner is None:
            raise JobLeaseLost(job_id)

    async def _lock_parse_checkpoint(
        self,
        job: JobIntent | _JobLeaseSnapshot,
        document_id: UUID,
        *,
        source_id: UUID,
        drive_file: DriveFile | None = None,
    ) -> Document:
        source = await self._db_session.scalar(
            select(DriveSource).where(DriveSource.id == source_id).with_for_update()
        )
        if source is None or source.status is not DriveSourceStatus.ACTIVE:
            raise DocumentParseError("DOCUMENT_REVOKED")
        parse_jobs = list(
            (
                await self._db_session.scalars(
                    select(JobIntent)
                    .where(
                        JobIntent.kind == "knowledge.document.parse",
                        JobIntent.payload["document_id"].as_string()
                        == str(document_id),
                    )
                    .order_by(JobIntent.id)
                    .with_for_update()
                )
            ).all()
        )
        locked_job = next((item for item in parse_jobs if item.id == job.id), None)
        if locked_job is None:
            raise JobLeaseLost(job.id)
        await self._assert_active_lease(locked_job)
        document = await self._db_session.scalar(
            select(Document)
            .where(
                Document.id == document_id,
                Document.organization_id == source.organization_id,
                Document.knowledge_base_id == source.knowledge_base_id,
                Document.source_id == source.id,
            )
            .with_for_update()
        )
        if document is None:
            raise DocumentParseError("DOCUMENT_NOT_FOUND")
        if (
            source.organization_id != document.organization_id
            or (
                drive_file is not None
                and not KnowledgeSourceService.is_file_authorized(source, drive_file)
            )
        ):
            raise DocumentParseError("DOCUMENT_REVOKED")
        return document

    def _prepare_version(
        self,
        content: bytes,
        mime_type: str,
        parser: DocumentParser | None = None,
        *,
        content_sha256: str | None = None,
    ) -> _PreparedVersion:
        version_id = uuid4()
        selected_parser = parser or self._parser_for(mime_type)
        try:
            sections = selected_parser.parse(content)
            chunks = self._chunker.chunk(
                document_version_id=version_id,
                sections=sections,
            )
        except DocumentParseError:
            raise
        except Exception as exc:
            raise DocumentParseError() from exc
        return _PreparedVersion(
            id=version_id,
            content_sha256=content_sha256 or sha256(content).hexdigest(),
            chunks=tuple(chunks),
        )

    async def _lock_versions_and_find_match(
        self, document_id: UUID, content_sha256: str
    ) -> tuple[DocumentVersion | None, list[DocumentVersion]]:
        versions = list(
            (
                await self._db_session.scalars(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document_id)
                    .order_by(DocumentVersion.id)
                    .with_for_update()
                )
            ).all()
        )
        return (
            next(
                (
                    version
                    for version in versions
                    if version.content_sha256 == content_sha256
                ),
                None,
            ),
            versions,
        )

    async def _reuse_or_reject_matching_version(
        self,
        lease_service: JobLeaseService,
        job: JobIntent,
        document: Document,
        version: DocumentVersion,
        locked_versions: list[DocumentVersion],
        *,
        expected_job_version: int,
    ) -> DocumentVersion:
        if version.state is not DocumentVersionState.RETRIEVABLE:
            error_codes = {
                DocumentVersionState.PROCESSING: "DOCUMENT_CONTENT_PROCESSING",
                DocumentVersionState.FAILED: "DOCUMENT_CONTENT_FAILED",
                DocumentVersionState.REVOKED: "DOCUMENT_CONTENT_REVOKED",
                DocumentVersionState.DELETED: "DOCUMENT_CONTENT_DELETED",
            }
            raise DocumentParseError(error_codes[version.state])
        relevant_version_ids = {version.id}
        if document.current_version_id is not None:
            relevant_version_ids.add(document.current_version_id)
        locked_chunks = list(
            (
                await self._db_session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_version_id.in_(relevant_version_ids))
                    .order_by(DocumentChunk.document_version_id, DocumentChunk.id)
                    .with_for_update()
                )
            ).all()
        )
        await self._transition_current_version(
            job,
            document,
            version,
            locked_versions,
            locked_chunks,
        )
        job.payload = {**job.payload, "document_version_id": str(version.id)}
        await self._db_session.flush()
        await lease_service.complete(
            job.id,
            self._worker_id or "",
            expected_version=expected_job_version,
        )
        return version

    async def _transition_current_version(
        self,
        job: JobIntent | _JobLeaseSnapshot,
        document: Document,
        target: DocumentVersion,
        locked_versions: list[DocumentVersion],
        locked_chunks: list[DocumentChunk],
    ) -> None:
        if target.document_id != document.id:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        previous_version_id = document.current_version_id
        if previous_version_id == target.id:
            return
        previous = next(
            (
                version
                for version in locked_versions
                if version.id == previous_version_id
            ),
            None,
        )
        if previous_version_id is not None and previous is None:
            raise DocumentParseError("DOCUMENT_CURRENT_VERSION_INVALID")
        if previous is not None and previous.state is not DocumentVersionState.RETRIEVABLE:
            raise DocumentParseError("DOCUMENT_CURRENT_VERSION_INVALID")

        document.current_version_id = target.id
        if previous is None:
            return
        previous.state = DocumentVersionState.REVOKED
        locked_old_chunk_ids = {
            chunk.id
            for chunk in locked_chunks
            if chunk.document_version_id == previous.id
        }
        deleted_chunk_ids = list(
            (
                await self._db_session.scalars(
                    delete(DocumentChunk)
                    .where(DocumentChunk.document_version_id == previous.id)
                    .returning(DocumentChunk.id)
                )
            ).all()
        )
        if set(deleted_chunk_ids) != locked_old_chunk_ids:
            raise DocumentParseError("DOCUMENT_VERSION_CHUNKS_CHANGED")
        details = {
            "organization_id": str(document.organization_id),
            "source_id": str(document.source_id),
            "document_id": str(document.id),
            "job_id": str(job.id),
            "old_version_id": str(previous.id),
            "new_version_id": str(target.id),
            "deleted_chunk_count": len(deleted_chunk_ids),
        }
        await self._audit_service.record_actor(
            self._db_session,
            organization_id=document.organization_id,
            actor_id=knowledge_worker_actor_id(document.organization_id),
            action="knowledge.document.version.replaced",
            object_type="document",
            object_id=document.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=tuple(details),
        )

    async def _persist_prepared_version(
        self, document: Document, prepared: _PreparedVersion
    ) -> DocumentVersion:
        version = DocumentVersion(
            id=prepared.id,
            document_id=document.id,
            state=DocumentVersionState.PROCESSING,
            content_sha256=prepared.content_sha256,
        )
        self._db_session.add(version)
        await self._db_session.flush()
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
            for chunk in prepared.chunks
        )
        await self._db_session.flush()
        return version

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
        version = await self._version_from_job(job)
        if version is None or version.state is not DocumentVersionState.RETRIEVABLE:
            raise DocumentParseError("DOCUMENT_PARSE_JOB_UNAVAILABLE")
        return version

    async def _commit_processing_checkpoint(
        self,
        job: JobIntent,
        version: DocumentVersion,
    ) -> None:
        job_id = job.id
        claimed_job_version = job.version
        checkpoint_payload = {**job.payload, "document_version_id": str(version.id)}
        checkpointed_job_id = await self._db_session.scalar(
            update(JobIntent)
            .where(
                JobIntent.id == job_id,
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_owner == self._worker_id,
                JobIntent.version == claimed_job_version,
                JobIntent.lease_expires_at.is_not(None),
                # clock_timestamp() is the PostgreSQL wall clock. CURRENT_TIMESTAMP
                # is fixed at transaction start and cannot fence a long parse.
                JobIntent.lease_expires_at > func.clock_timestamp(),
            )
            .values(payload=checkpoint_payload)
            .returning(JobIntent.id)
        )
        if checkpointed_job_id is None:
            await self._db_session.rollback()
            raise JobLeaseLost(job_id)
        # The conditional UPDATE and version/chunk inserts commit together. PostgreSQL
        # row locking plus predicate re-evaluation fences both expiry and takeover.
        await self._db_session.commit()

    async def _version_from_job(self, job: JobIntent) -> DocumentVersion | None:
        raw_version_id = job.payload.get("document_version_id")
        if not isinstance(raw_version_id, str):
            return None
        try:
            version_id = UUID(raw_version_id)
        except ValueError as exc:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB") from exc
        version = await self._db_session.get(DocumentVersion, version_id)
        if version is None:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        document_id = self._document_id_from_payload(job.payload)
        if version.document_id != document_id:
            raise DocumentParseError("INVALID_DOCUMENT_PARSE_JOB")
        return version

    async def _fail_terminal(
        self,
        lease_service: JobLeaseService,
        job_id: UUID,
        expected_version: int,
        error_code: str,
    ) -> None:
        await lease_service.retry(
            job_id,
            self._worker_id or "",
            error_code=error_code,
            error_class=ErrorClass.NON_RETRYABLE,
            expected_version=expected_version,
        )
        await self._db_session.commit()
