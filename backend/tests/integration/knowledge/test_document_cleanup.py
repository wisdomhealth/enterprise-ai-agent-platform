from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.modules.identity.models import Organization
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.tasks import _consume_document_cleanup_event
from app.modules.outbox.models import OutboxEvent, ProcessedEvent


async def _document_with_chunk(
    db_session,  # type: ignore[no-untyped-def]
    *,
    owner: str,
    external_id: str,
    title: str = "policy.pdf",
    state: DocumentVersionState = DocumentVersionState.REVOKED,
) -> tuple[Organization, DriveSource, Document, DocumentVersion, DocumentChunk]:
    organization = Organization(name=owner)
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
        connection_identity=f"{external_id}@example.test",
        sync_cursor="cursor",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id=external_id,
        title=title,
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    version = DocumentVersion(
        document_id=document.id,
        state=state,
        content_sha256=uuid4().hex + uuid4().hex,
    )
    db_session.add(version)
    await db_session.flush()
    if state is DocumentVersionState.RETRIEVABLE:
        document.current_version_id = version.id
    chunk = DocumentChunk(
        id=uuid4(),
        document_version_id=version.id,
        ordinal=0,
        text=f"chunk for {external_id}",
        page_number=1,
        section=None,
        token_count=3,
        metadata_={},
        embedding=[0.0] * 1536,
    )
    db_session.add(chunk)
    await db_session.flush()
    return organization, source, document, version, chunk


def _cleanup_event(
    *, organization_id: UUID, source_id: UUID, document_id: UUID, version_ids: list[UUID]
) -> OutboxEvent:
    return OutboxEvent(
        event_type="knowledge.document.cleanup.requested",
        aggregate_type="document",
        aggregate_id=document_id,
        payload={
            "organization_id": str(organization_id),
            "source_id": str(source_id),
            "document_id": str(document_id),
            "version_ids": [str(version_id) for version_id in version_ids],
        },
    )


@pytest.mark.asyncio
async def test_cleanup_deletes_only_requested_revoked_version_chunks_and_is_idempotent(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    organization, source, old_document, old_version, old_chunk = await _document_with_chunk(
        db_session, owner="cleanup owner", external_id="old-drive-id"
    )
    new_document = Document(
        organization_id=organization.id,
        knowledge_base_id=old_document.knowledge_base_id,
        source_id=source.id,
        external_id="new-drive-id",
        title=old_document.title,
        mime_type="application/pdf",
    )
    db_session.add(new_document)
    await db_session.flush()
    new_version = DocumentVersion(
        document_id=new_document.id,
        state=DocumentVersionState.RETRIEVABLE,
        content_sha256=uuid4().hex + uuid4().hex,
    )
    db_session.add(new_version)
    await db_session.flush()
    new_document.current_version_id = new_version.id
    new_chunk = DocumentChunk(
        id=uuid4(),
        document_version_id=new_version.id,
        ordinal=0,
        text="replacement chunk",
        page_number=1,
        section=None,
        token_count=2,
        metadata_={},
        embedding=[0.0] * 1536,
    )
    db_session.add(new_chunk)
    _, _, other_document, other_version, other_chunk = await _document_with_chunk(
        db_session, owner="other owner", external_id="other-drive-id"
    )
    event = _cleanup_event(
        organization_id=organization.id,
        source_id=source.id,
        document_id=old_document.id,
        version_ids=[old_version.id],
    )
    db_session.add(event)
    await db_session.flush()
    event_id = event.event_id
    old_document_id = old_document.id
    old_version_id = old_version.id
    old_chunk_id = old_chunk.id
    new_chunk_id = new_chunk.id
    new_version_id = new_version.id
    other_chunk_id = other_chunk.id
    new_document_id = new_document.id
    other_document_id = other_document.id
    other_version_id = other_version.id
    await db_session.commit()

    assert await _consume_document_cleanup_event(event_id, db_session=db_session) is True
    assert await _consume_document_cleanup_event(event_id, db_session=db_session) is False

    assert await db_session.get(DocumentChunk, old_chunk_id) is None
    assert await db_session.get(DocumentChunk, new_chunk_id) is not None
    assert await db_session.get(DocumentChunk, other_chunk_id) is not None
    assert await db_session.get(Document, old_document_id) is not None
    persisted_old_version = await db_session.get(DocumentVersion, old_version_id)
    assert persisted_old_version is not None
    assert persisted_old_version.state is DocumentVersionState.REVOKED
    persisted_new_document = await db_session.get(Document, new_document_id)
    assert persisted_new_document is not None
    assert persisted_new_document.external_id == "new-drive-id"
    assert persisted_new_document.title == "policy.pdf"
    persisted_new_version = await db_session.get(DocumentVersion, new_version_id)
    assert persisted_new_version is not None
    assert persisted_new_version.state is DocumentVersionState.RETRIEVABLE
    assert other_document_id != old_document_id
    assert other_version_id != old_version_id
    assert new_document_id != old_document_id


@pytest.mark.asyncio
async def test_failed_cleanup_rolls_back_marker_and_can_be_retried(db_session) -> None:  # type: ignore[no-untyped-def]
    organization, source, document, version, chunk = await _document_with_chunk(
        db_session,
        owner="retry cleanup owner",
        external_id="retry-drive-id",
        state=DocumentVersionState.PROCESSING,
    )
    event = _cleanup_event(
        organization_id=organization.id,
        source_id=source.id,
        document_id=document.id,
        version_ids=[version.id],
    )
    db_session.add(event)
    await db_session.flush()
    event_id = event.event_id
    version_id = version.id
    chunk_id = chunk.id
    await db_session.commit()

    with pytest.raises(ValueError, match="revoked versions"):
        await _consume_document_cleanup_event(event_id, db_session=db_session)

    assert await db_session.scalar(select(func.count(ProcessedEvent.event_id))) == 0
    assert await db_session.get(DocumentChunk, chunk_id) is not None
    version = await db_session.get(DocumentVersion, version_id)
    assert version is not None
    version.state = DocumentVersionState.REVOKED
    await db_session.commit()

    assert await _consume_document_cleanup_event(event_id, db_session=db_session) is True
    assert await db_session.get(DocumentChunk, chunk_id) is None


@pytest.mark.asyncio
async def test_cleanup_rejects_cross_owner_version_ids_without_deleting_chunks(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    organization, source, document, version, chunk = await _document_with_chunk(
        db_session, owner="scoped cleanup owner", external_id="scoped-drive-id"
    )
    _, _, _, foreign_version, foreign_chunk = await _document_with_chunk(
        db_session, owner="foreign cleanup owner", external_id="foreign-drive-id"
    )
    event = _cleanup_event(
        organization_id=organization.id,
        source_id=source.id,
        document_id=document.id,
        version_ids=[version.id, foreign_version.id],
    )
    db_session.add(event)
    await db_session.flush()
    event_id = event.event_id
    chunk_id = chunk.id
    foreign_chunk_id = foreign_chunk.id
    await db_session.commit()

    with pytest.raises(ValueError, match="ownership mismatch"):
        await _consume_document_cleanup_event(event_id, db_session=db_session)

    assert await db_session.get(DocumentChunk, chunk_id) is not None
    assert await db_session.get(DocumentChunk, foreign_chunk_id) is not None
