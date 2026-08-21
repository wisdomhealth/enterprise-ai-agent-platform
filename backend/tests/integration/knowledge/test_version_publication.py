from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService
from app.modules.knowledge.drive_gateway import DriveConnection, DriveGateway
from app.modules.knowledge.ingestion import DocumentIngestionService
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.parsers import DocumentParseError
from app.modules.knowledge.service import KnowledgeSourceService

FIXTURE_DIRECTORY = Path("tests/fixtures/documents")


class FailingParser:
    def parse(self, content: bytes):  # type: ignore[no-untyped-def]
        raise DocumentParseError("DOCUMENT_PARSE_FAILED")


class FakeDriveGateway(DriveGateway):
    def __init__(self, content: bytes, *, error: Exception | None = None) -> None:
        super().__init__()
        self._content = content
        self._error = error
        self.download_calls: list[str] = []

    async def download(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        if self._error is not None:
            raise self._error
        return self._content


class FakeDriveGatewayFactory:
    def __init__(self, gateway: FakeDriveGateway) -> None:
        self._gateway = gateway

    async def create(self, *, refresh_token: str) -> DriveConnection:
        return DriveConnection(gateway=self._gateway, connection_identity="reader@example.test")


class LeaseLostOnceService(JobLeaseService):
    def __init__(self, db_session) -> None:
        super().__init__(db_session)
        self._lose_next_completion = True

    async def complete(self, job_id, worker_id):  # type: ignore[no-untyped-def]
        if self._lose_next_completion:
            self._lose_next_completion = False
            raise JobLeaseLost(job_id)
        return await super().complete(job_id, worker_id)


async def _document(db_session, *, current_is_retrievable: bool) -> Document:
    organization = Organization(name=f"document ingestion {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="source-root",
        allowed_descendant_ids=[],
        connection_identity="reader@example.test",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id="drive-file-1",
        title="Customer policy",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    if current_is_retrievable:
        current = DocumentVersion(
            document_id=document.id,
            state=DocumentVersionState.RETRIEVABLE,
            content_sha256="a" * 64,
        )
        db_session.add(current)
        await db_session.flush()
        document.current_version_id = current.id
        await db_session.flush()
    return document


async def _authorized_ingestion_service(
    db_session,
    document: Document,
    tmp_path: Path,
    *,
    content: bytes | None = None,
    download_error: Exception | None = None,
    job_lease_service: JobLeaseService | None = None,
):
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"k" * 32)
    connector_service = ConnectorService.for_file_key(key_path, app_env="development")
    secret = await connector_service.store_refresh_token(
        db_session,
        organization_id=document.organization_id,
        refresh_token="test-only-refresh-token",
    )
    db_session.add(
        Connector(
            organization_id=document.organization_id,
            kind=ConnectorKind.DRIVE,
            status=ConnectorStatus.ACTIVE,
            secret_id=secret.id,
        )
    )
    await db_session.flush()
    gateway = FakeDriveGateway(
        content or (FIXTURE_DIRECTORY / "sample.pdf").read_bytes(),
        error=download_error,
    )
    source_service = KnowledgeSourceService(
        connector_service, FakeDriveGatewayFactory(gateway)
    )
    return DocumentIngestionService(
        db_session,
        knowledge_source_service=source_service,
        worker_id="task-8-test-worker",
        job_lease_service=job_lease_service,
    ), gateway


def _drive_file_payload(*, parent_id: str) -> dict[str, object]:
    return {
        "id": "drive-file-1",
        "name": "policy.pdf",
        "mime_type": "application/pdf",
        "modified_time": datetime.now(UTC).isoformat(),
        "parent_ids": [parent_id],
        "web_view_link": None,
        "removed": False,
    }


@pytest.mark.asyncio
async def test_failed_new_version_keeps_previous_version_retrievable(db_session) -> None:
    document = await _document(db_session, current_is_retrievable=True)
    service = DocumentIngestionService(db_session)

    with pytest.raises(DocumentParseError):
        await service.ingest_bytes(document, b"invalid", "application/pdf", FailingParser())

    await db_session.refresh(document)
    assert document.current_version_id is not None
    current = await db_session.get(DocumentVersion, document.current_version_id)
    assert current is not None
    assert current.state is DocumentVersionState.RETRIEVABLE
    failed_versions = (
        await db_session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.state == DocumentVersionState.FAILED,
            )
        )
    ).all()
    assert [version.error_code for version in failed_versions] == ["DOCUMENT_PARSE_FAILED"]


@pytest.mark.asyncio
async def test_parsed_version_is_not_retrievable_before_embedding(db_session) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    service = DocumentIngestionService(db_session)

    version = await service.parse_bytes(
        document,
        (FIXTURE_DIRECTORY / "sample.pdf").read_bytes(),
        "application/pdf",
    )

    assert version.state is DocumentVersionState.PROCESSING
    assert document.current_version_id != version.id
    chunks = await db_session.scalars(
        select(DocumentVersion).where(DocumentVersion.id == version.id)
    )
    assert chunks.one().state is DocumentVersionState.PROCESSING


@pytest.mark.asyncio
async def test_parse_job_downloads_authorized_file_and_persists_processing_chunks(
    db_session, tmp_path
) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    source = await db_session.get(DriveSource, document.source_id)
    assert source is not None
    source.allowed_descendant_ids = ["authorized-folder"]
    service, gateway = await _authorized_ingestion_service(db_session, document, tmp_path)
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        "document-parse-authorized-file",
        {
            "document_id": str(document.id),
            "drive_file": _drive_file_payload(parent_id="authorized-folder"),
        },
    )

    version = await service.parse(job.id)
    repeated_version = await service.parse(job.id)

    await db_session.refresh(job)
    assert gateway.download_calls == ["drive-file-1"]
    assert repeated_version.id == version.id
    assert version.state is DocumentVersionState.PROCESSING
    assert document.current_version_id != version.id
    assert job.state is JobState.SUCCEEDED
    assert job.payload["document_version_id"] == str(version.id)
    chunks = (
        await db_session.scalars(
            select(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
        )
    ).all()
    assert chunks


@pytest.mark.asyncio
async def test_parse_job_rejects_unauthorized_file_before_drive_download(
    db_session, tmp_path
) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    service, gateway = await _authorized_ingestion_service(db_session, document, tmp_path)
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        "document-parse-unauthorized-file",
        {
            "document_id": str(document.id),
            "drive_file": _drive_file_payload(parent_id="private-folder"),
        },
    )

    with pytest.raises(HTTPException) as error:
        await service.parse(job.id)

    await db_session.refresh(job)
    assert error.value.status_code == 403
    assert gateway.download_calls == []
    assert job.state is JobState.FAILED


@pytest.mark.asyncio
async def test_parse_job_rejects_payload_file_id_that_does_not_match_document(
    db_session, tmp_path
) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    service, gateway = await _authorized_ingestion_service(db_session, document, tmp_path)
    mismatched_payload = _drive_file_payload(parent_id="source-root")
    mismatched_payload["id"] = "unrelated-file"
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        "document-parse-mismatched-file",
        {
            "document_id": str(document.id),
            "drive_file": mismatched_payload,
        },
    )

    with pytest.raises(DocumentParseError) as error:
        await service.parse(job.id)

    await db_session.refresh(job)
    assert error.value.code == "DOCUMENT_FILE_MISMATCH"
    assert gateway.download_calls == []
    assert job.state is JobState.FAILED


@pytest.mark.asyncio
async def test_parse_job_marks_parser_failure_terminal(db_session, tmp_path) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    source = await db_session.get(DriveSource, document.source_id)
    assert source is not None
    source.allowed_descendant_ids = ["authorized-folder"]
    service, gateway = await _authorized_ingestion_service(
        db_session,
        document,
        tmp_path,
        content=b"not-a-pdf",
    )
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        "document-parse-invalid-pdf",
        {
            "document_id": str(document.id),
            "drive_file": _drive_file_payload(parent_id="authorized-folder"),
        },
    )

    with pytest.raises(DocumentParseError):
        await service.parse(job.id)

    await db_session.refresh(job)
    assert gateway.download_calls == ["drive-file-1"]
    assert job.state is JobState.FAILED


@pytest.mark.asyncio
async def test_parse_job_does_not_duplicate_an_active_lease(db_session, tmp_path) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    source = await db_session.get(DriveSource, document.source_id)
    assert source is not None
    source.allowed_descendant_ids = ["authorized-folder"]
    service, gateway = await _authorized_ingestion_service(db_session, document, tmp_path)
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        "document-parse-duplicate-lease",
        {
            "document_id": str(document.id),
            "drive_file": _drive_file_payload(parent_id="authorized-folder"),
        },
    )
    claimed = await JobLeaseService(db_session).claim(job.id, "another-worker", lease_seconds=60)
    assert claimed is not None

    with pytest.raises(DocumentParseError) as error:
        await service.parse(job.id)

    await db_session.refresh(job)
    assert error.value.code == "DOCUMENT_PARSE_JOB_UNAVAILABLE"
    assert gateway.download_calls == []
    assert job.state is JobState.RUNNING


@pytest.mark.asyncio
async def test_parse_job_records_retryable_download_failure(db_session, tmp_path) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    source = await db_session.get(DriveSource, document.source_id)
    assert source is not None
    source.allowed_descendant_ids = ["authorized-folder"]
    service, gateway = await _authorized_ingestion_service(
        db_session,
        document,
        tmp_path,
        download_error=RuntimeError("temporary Drive outage"),
    )
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        "document-parse-retryable-download-failure",
        {
            "document_id": str(document.id),
            "drive_file": _drive_file_payload(parent_id="authorized-folder"),
        },
    )

    with pytest.raises(RuntimeError, match="temporary Drive outage"):
        await service.parse(job.id)

    await db_session.refresh(job)
    assert gateway.download_calls == ["drive-file-1"]
    assert job.state is JobState.PENDING
    assert job.last_error_code == "DOCUMENT_PARSE_TRANSIENT_FAILURE"
    assert job.next_attempt_at is not None


@pytest.mark.asyncio
async def test_parse_job_recovers_after_completion_lease_loss_without_duplicate_ingestion(
    db_session, tmp_path
) -> None:
    document = await _document(db_session, current_is_retrievable=False)
    source = await db_session.get(DriveSource, document.source_id)
    assert source is not None
    source.allowed_descendant_ids = ["authorized-folder"]
    lease_service = LeaseLostOnceService(db_session)
    service, gateway = await _authorized_ingestion_service(
        db_session,
        document,
        tmp_path,
        job_lease_service=lease_service,
    )
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        "document-parse-completion-lease-loss",
        {
            "document_id": str(document.id),
            "drive_file": _drive_file_payload(parent_id="authorized-folder"),
        },
    )

    with pytest.raises(JobLeaseLost):
        await service.parse(job.id)

    await db_session.refresh(job)
    version_id = job.payload["document_version_id"]
    assert job.state is JobState.RUNNING
    assert isinstance(version_id, str)
    versions = (
        await db_session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document.id)
        )
    ).all()
    assert len(versions) == 1
    chunks_before = (
        await db_session.scalars(
            select(DocumentChunk).where(DocumentChunk.document_version_id == versions[0].id)
        )
    ).all()
    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    recovered_version = await service.parse(job.id)

    await db_session.refresh(job)
    chunks_after = (
        await db_session.scalars(
            select(DocumentChunk).where(DocumentChunk.document_version_id == versions[0].id)
        )
    ).all()
    assert recovered_version.id == versions[0].id
    assert str(recovered_version.id) == version_id
    assert job.state is JobState.SUCCEEDED
    assert gateway.download_calls == ["drive-file-1"]
    assert len(chunks_after) == len(chunks_before)
