from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.drive_gateway import DriveFile, DriveGateway
from app.modules.knowledge.service import KnowledgeSourceService


class FakeDriveGateway(DriveGateway):
    async def resolve_descendant_folder_ids(self, root_folder_id: str) -> set[str]:
        assert root_folder_id == "approved-root"
        return {"approved-child"}


async def _principal_for(
    db_session, organization: Organization, *, role: UserRole, email: str
) -> Principal:
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"knowledge-test-{uuid4()}",
        email=email,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    return Principal(
        subject_id=user.id,
        organization_id=organization.id,
        email=user.email,
        role=user.role,
        session_id=uuid4(),
        csrf_hash="csrf",
    )


@pytest.mark.asyncio
async def test_only_admin_with_knowledge_write_grant_can_configure_drive_root(
    db_session,
) -> None:
    organization = Organization(name="Knowledge source owner")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    service = KnowledgeSourceService(FakeDriveGateway())
    resource_id = service.configuration_resource_id(organization.id)
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=principal.subject_id,
            resource_type="knowledge",
            resource_id=resource_id,
            actions=["knowledge.write"],
        )
    )
    await db_session.flush()

    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id="approved-root",
        connection_identity="knowledge-reader@example.test",
    )

    assert source.organization_id == organization.id
    assert source.root_folder_id == "approved-root"
    assert source.allowed_descendant_ids == ["approved-child"]


@pytest.mark.asyncio
async def test_member_cannot_configure_drive_root_even_with_knowledge_write_grant(
    db_session,
) -> None:
    organization = Organization(name="Knowledge source member")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.MEMBER, email="member@example.test"
    )
    service = KnowledgeSourceService(FakeDriveGateway())
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=principal.subject_id,
            resource_type="knowledge",
            resource_id=service.configuration_resource_id(organization.id),
            actions=["knowledge.write"],
        )
    )
    await db_session.flush()

    with pytest.raises(HTTPException) as error:
        await service.configure_drive_source(
            db_session,
            principal=principal,
            root_folder_id="approved-root",
            connection_identity="knowledge-reader@example.test",
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_gateway_download_is_blocked_outside_configured_drive_scope(db_session) -> None:
    organization = Organization(name="Knowledge scope enforcement")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    service = KnowledgeSourceService(FakeDriveGateway())
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=principal.subject_id,
            resource_type="knowledge",
            resource_id=service.configuration_resource_id(organization.id),
            actions=["knowledge.write"],
        )
    )
    await db_session.flush()
    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id="approved-root",
        connection_identity="knowledge-reader@example.test",
    )
    foreign_file = DriveFile(
        id="outside-file",
        name="private.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        modified_time=None,
        parent_ids=("private-root",),
        web_view_link=None,
        removed=False,
    )

    assert service.is_file_authorized(source, foreign_file) is False
