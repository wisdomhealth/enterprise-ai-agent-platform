from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fakes.providers import DriveFixture, HttpDriveChangeBoundary
from sqlalchemy import select

from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.sync import DriveSyncService
from app.modules.rag.text_search import TextCandidateSource
from app.modules.rag.vector_search import VectorCandidateSource


@pytest.mark.asyncio
async def test_revoked_drive_chunk_never_appears_in_either_retrieval_branch(
    db_session, provider_stack
) -> None:  # type: ignore[no-untyped-def]
    organization = Organization(name=f"Task 26 Drive {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"task26-{uuid4()}",
        email="staff@example.test",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    knowledge = KnowledgeBase(organization_id=organization.id)
    db_session.add_all((user, knowledge))
    await db_session.flush()
    staff_session = StaffSession(
        user_id=user.id,
        csrf_hash="task26",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge.id,
        root_folder_id="approved-root",
        connection_identity="reader@example.test",
    )
    db_session.add_all((staff_session, source))
    await db_session.flush()
    principal = Principal(
        user.id, organization.id, user.email, user.role, staff_session.id, "task26"
    )
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=user.id,
            resource_type="knowledge",
            resource_id=knowledge.id,
            actions=["knowledge.read"],
        )
    )
    provider_stack.add_drive_file(
        DriveFixture(
            "revoked-policy",
            "Private policy",
            "application/pdf",
            b"private policy fixture",
        )
    )
    provider_stack.state.drive_pages["drive-start-1"] = (["revoked-policy"], None)
    boundary = HttpDriveChangeBoundary(provider_stack)
    await DriveSyncService(db_session, page_gateway=boundary).sync(source.id)
    document = await db_session.scalar(
        select(Document).where(Document.external_id == "revoked-policy")
    )
    assert document is not None
    version = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.RETRIEVABLE,
        content_sha256="a" * 64,
    )
    db_session.add(version)
    await db_session.flush()
    document.current_version_id = version.id
    chunk = DocumentChunk(
        id=uuid4(),
        document_version_id=version.id,
        ordinal=0,
        text="Private policy",
        page_number=1,
        section="Policy",
        token_count=2,
        metadata_={"source": "drive"},
        embedding=[1.0] * 1536,
    )
    db_session.add(chunk)
    await db_session.flush()

    provider_stack.revoke_drive_file("revoked-policy")
    provider_stack.state.drive_pages["drive-start-1"] = (["revoked-policy"], None)
    await DriveSyncService(db_session, page_gateway=boundary).sync(
        source.id, page_token="drive-start-1"
    )
    await db_session.refresh(version)
    assert version.state is DocumentVersionState.REVOKED
    vector = await VectorCandidateSource(db_session).search(
        principal, knowledge.id, "Private policy", 10, query_embedding=[1.0] * 1536
    )
    text = await TextCandidateSource(db_session).search(
        principal, knowledge.id, "Private policy", 10
    )

    assert chunk.id not in {candidate.chunk_id for candidate in vector}
    assert chunk.id not in {candidate.chunk_id for candidate in text}
