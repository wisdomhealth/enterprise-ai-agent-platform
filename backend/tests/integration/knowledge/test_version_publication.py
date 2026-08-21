from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.identity.models import Organization
from app.modules.jobs.service import JobService
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
    def __init__(self, content: bytes) -> None:
        super().__init__()
        self._content = content
        self.download_calls: list[str] = []

    async def download(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        return self._content


class FakeDriveGatewayFactory:
    def __init__(self, gateway: FakeDriveGateway) -> None:
        self._gateway = gateway

    async def create(self, *, refresh_token: str) -> DriveConnection:
        return DriveConnection(gateway=self._gateway, connection_identity="reader@example.test")


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
        external_id=f"drive-{uuid4()}",
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


async def _authorized_ingestion_service(db_session, document: Document, tmp_path: Path):
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
    gateway = FakeDriveGateway((FIXTURE_DIRECTORY / "sample.pdf").read_bytes())
    source_service = KnowledgeSourceService(
        connector_service, FakeDriveGatewayFactory(gateway)
    )
    return DocumentIngestionService(
        db_session,
        knowledge_source_service=source_service,
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

    assert gateway.download_calls == ["drive-file-1"]
    assert version.state is DocumentVersionState.PROCESSING
    assert document.current_version_id != version.id
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

    assert error.value.status_code == 403
    assert gateway.download_calls == []
