from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.identity.models import Organization
from app.modules.knowledge.drive_gateway import DriveFile
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.sync import DriveSyncService


class AuthorizationBoundary:
    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        return [
            DriveFile(
                id="removed-file",
                name="",
                mime_type="",
                modified_time=None,
                parent_ids=(),
                web_view_link=None,
                removed=True,
            )
        ], "cursor-2"

    @staticmethod
    def is_file_authorized(source, drive_file):  # type: ignore[no-untyped-def]
        return source.root_folder_id in drive_file.parent_ids


class _CredentialRejected(Exception):
    status_code = 401


class CredentialFailureBoundary:
    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        raise _CredentialRejected()


class TransientFailureBoundary:
    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        raise RuntimeError("temporary Drive failure")


class EmptyRetryBoundary:
    async def list_changes(self, _db_session, *, source, sync_cursor):  # type: ignore[no-untyped-def]
        return [], "cursor-2"


@pytest.mark.asyncio
async def test_detected_folder_removal_revokes_before_cleanup(db_session) -> None:  # type: ignore[no-untyped-def]
    organization = Organization(name="Authorization loss owner")
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
        sync_cursor="cursor-1",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id="removed-file",
        title="old file",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    version = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.RETRIEVABLE,
        content_sha256="a" * 64,
    )
    db_session.add(version)
    await db_session.flush()
    version_id = version.id
    document_id = document.id
    document.current_version_id = version.id
    db_session.add(
        DocumentChunk(
            id=uuid4(),
            document_version_id=version.id,
            ordinal=0,
            text="still retained until cleanup",
            page_number=1,
            section=None,
            token_count=5,
            metadata_={},
        )
    )
    await db_session.commit()

    await DriveSyncService(db_session, page_gateway=AuthorizationBoundary()).sync(
        source.id, "cursor-1"
    )

    db_session.expire_all()
    persisted_version = await db_session.get(DocumentVersion, version_id)
    persisted_document = await db_session.get(Document, document_id)
    assert persisted_version is not None
    assert persisted_version.state is DocumentVersionState.REVOKED
    assert persisted_document is not None
    assert persisted_document.current_version_id is None
    assert await db_session.scalar(select(func.count(DocumentChunk.id))) == 1


@pytest.mark.asyncio
async def test_invalid_drive_credentials_require_reauthorization(db_session) -> None:  # type: ignore[no-untyped-def]
    organization = Organization(name="Credential failure owner")
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
        sync_cursor="cursor-1",
    )
    secret = ConnectorSecret(
        organization_id=organization.id,
        ciphertext=b"ciphertext",
        encrypted_data_key=b"wrapped-key",
        nonce=b"nonce",
        algorithm="AES-GCM",
        key_version="test-key",
    )
    db_session.add_all((source, secret))
    await db_session.flush()
    connector = Connector(
        organization_id=organization.id,
        kind=ConnectorKind.DRIVE,
        status=ConnectorStatus.ACTIVE,
        secret_id=secret.id,
    )
    db_session.add(connector)
    await db_session.commit()
    source_id = source.id
    connector_id = connector.id

    result = await DriveSyncService(db_session, page_gateway=CredentialFailureBoundary()).sync(
        source_id, "cursor-1"
    )

    db_session.expire_all()
    persisted_source = await db_session.get(DriveSource, source_id)
    persisted_connector = await db_session.get(Connector, connector_id)
    assert result.reauth_required is True
    assert persisted_source is not None
    assert persisted_source.status.value == "ERROR"
    assert persisted_connector is not None
    assert persisted_connector.status is ConnectorStatus.REAUTH_REQUIRED


@pytest.mark.asyncio
async def test_transient_drive_failure_keeps_source_retryable(db_session) -> None:  # type: ignore[no-untyped-def]
    organization = Organization(name="Transient sync owner")
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
        sync_cursor="cursor-1",
    )
    db_session.add(source)
    await db_session.commit()
    source_id = source.id

    with pytest.raises(RuntimeError, match="temporary Drive failure"):
        await DriveSyncService(db_session, page_gateway=TransientFailureBoundary()).sync(
            source_id, "cursor-1"
        )
    await db_session.rollback()
    persisted = await db_session.get(DriveSource, source_id)
    assert persisted is not None
    assert persisted.status.value == "ACTIVE"

    retried = await DriveSyncService(db_session, page_gateway=EmptyRetryBoundary()).sync(
        source_id, "cursor-1"
    )
    assert retried.cursor == "cursor-2"
