from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.modules.identity.models import Organization
from app.modules.knowledge.ingestion import DocumentIngestionService
from app.modules.knowledge.models import (
    Document,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.parsers import DocumentParseError

FIXTURE_DIRECTORY = Path("tests/fixtures/documents")


class FailingParser:
    def parse(self, content: bytes):  # type: ignore[no-untyped-def]
        raise DocumentParseError("DOCUMENT_PARSE_FAILED")


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
