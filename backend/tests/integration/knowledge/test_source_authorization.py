from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.drive_gateway import DriveConnection, DriveFile, DriveGateway
from app.modules.knowledge.service import KnowledgeSourceService


class FakeDriveGateway(DriveGateway):
    def __init__(self) -> None:
        super().__init__()
        self.download_calls: list[str] = []

    async def resolve_descendant_folder_ids(self, root_folder_id: str) -> set[str]:
        return {f"{root_folder_id}-child"}

    async def download(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        return b"authorized content"


class FakeDriveGatewayFactory:
    def __init__(self, gateway: FakeDriveGateway) -> None:
        self._gateway = gateway
        self.refresh_tokens: list[str] = []
        self.connection_identity = "knowledge-reader@example.test"

    async def create(self, *, refresh_token: str) -> DriveConnection:
        self.refresh_tokens.append(refresh_token)
        return DriveConnection(
            gateway=self._gateway,
            connection_identity=self.connection_identity,
        )


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


async def _drive_connector_service(
    db_session, organization: Organization, tmp_path: Path
) -> ConnectorService:
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"k" * 32)
    connector_service = ConnectorService.for_file_key(key_path, app_env="development")
    secret = await connector_service.store_refresh_token(
        db_session, organization_id=organization.id, refresh_token="test-only-refresh-token"
    )
    db_session.add(
        Connector(
            organization_id=organization.id,
            kind=ConnectorKind.DRIVE,
            status=ConnectorStatus.ACTIVE,
            secret_id=secret.id,
        )
    )
    await db_session.flush()
    return connector_service


async def _configuration_grant(
    db_session, principal: Principal, service: KnowledgeSourceService
) -> None:
    db_session.add(
        ResourceGrant(
            organization_id=principal.organization_id,
            subject_id=principal.subject_id,
            resource_type="knowledge",
            resource_id=service.configuration_resource_id(principal.organization_id),
            actions=["knowledge.write"],
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_admin_configuration_uses_connector_identity_and_writes_safe_audit_event(
    db_session, tmp_path
) -> None:
    organization = Organization(name="Knowledge source owner")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    connector_service = await _drive_connector_service(db_session, organization, tmp_path)
    gateway = FakeDriveGateway()
    factory = FakeDriveGatewayFactory(gateway)
    service = KnowledgeSourceService(connector_service, factory)
    await _configuration_grant(db_session, principal, service)

    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id="approved-root",
    )

    audit_event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.object_id == source.id,
            AuditEvent.action == "knowledge.drive_source.configure",
        )
    )
    assert source.organization_id == organization.id
    assert source.root_folder_id == "approved-root"
    assert source.allowed_descendant_ids == ["approved-root-child"]
    assert source.connection_identity == "knowledge-reader@example.test"
    assert factory.refresh_tokens == ["test-only-refresh-token"]
    assert audit_event is not None
    assert audit_event.organization_id == organization.id
    assert audit_event.actor_id == principal.subject_id
    assert audit_event.details["connector_id"]
    assert audit_event.details["root_folder_ref"]
    assert audit_event.details["connection_identity_ref"]
    assert audit_event.details["include_descendants"] is True
    assert audit_event.details["changed_fields"]["root_folder_ref"]["before"] is None
    assert audit_event.details["changed_fields"]["root_folder_ref"]["after"] == audit_event.details[
        "root_folder_ref"
    ]
    assert "test-only-refresh-token" not in str(audit_event.details)


@pytest.mark.asyncio
async def test_reconfiguration_audit_reconstructs_actual_old_and_new_safe_references(
    db_session, tmp_path
) -> None:
    organization = Organization(name="Knowledge source audit update")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    connector_service = await _drive_connector_service(db_session, organization, tmp_path)
    factory = FakeDriveGatewayFactory(FakeDriveGateway())
    service = KnowledgeSourceService(connector_service, factory)
    await _configuration_grant(db_session, principal, service)
    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id="approved-root",
    )
    factory.connection_identity = "rotated-reader@example.test"
    await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id="replacement-root",
        include_descendants=False,
    )

    events = (
        await db_session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.object_id == source.id,
                AuditEvent.action == "knowledge.drive_source.configure",
            )
        )
    ).all()
    replacement_root_ref = sha256(b"replacement-root").hexdigest()[:16]
    first = next(
        event for event in events if event.details["root_folder_ref"] != replacement_root_ref
    )
    second = next(
        event for event in events if event.details["root_folder_ref"] == replacement_root_ref
    )
    changed = second.details["changed_fields"]

    assert second.organization_id == organization.id
    assert second.actor_id == principal.subject_id
    assert second.object_id == source.id
    assert changed["root_folder_ref"] == {
        "before": first.details["root_folder_ref"],
        "after": second.details["root_folder_ref"],
    }
    assert changed["connection_identity_ref"] == {
        "before": first.details["connection_identity_ref"],
        "after": second.details["connection_identity_ref"],
    }
    assert changed["connector_id"] == {
        "before": first.details["connector_id"],
        "after": second.details["connector_id"],
    }
    serialized = str(second.details).lower()
    for forbidden in (
        "refresh",
        "access_token",
        "token",
        "client_secret",
        "secret",
        "authorization",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_member_cannot_configure_drive_root_even_with_knowledge_write_grant(
    db_session, tmp_path
) -> None:
    organization = Organization(name="Knowledge source member")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.MEMBER, email="member@example.test"
    )
    connector_service = await _drive_connector_service(db_session, organization, tmp_path)
    service = KnowledgeSourceService(connector_service, FakeDriveGatewayFactory(FakeDriveGateway()))
    await _configuration_grant(db_session, principal, service)

    with pytest.raises(HTTPException) as error:
        await service.configure_drive_source(
            db_session,
            principal=principal,
            root_folder_id="approved-root",
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_out_of_scope_file_never_reaches_drive_download(db_session, tmp_path) -> None:
    organization = Organization(name="Knowledge scope enforcement")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    connector_service = await _drive_connector_service(db_session, organization, tmp_path)
    gateway = FakeDriveGateway()
    service = KnowledgeSourceService(connector_service, FakeDriveGatewayFactory(gateway))
    await _configuration_grant(db_session, principal, service)
    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id="approved-root",
    )
    foreign_file = DriveFile(
        id="outside-file",
        name="private.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        modified_time=datetime.now(UTC),
        parent_ids=("private-root",),
        web_view_link=None,
        removed=False,
    )

    with pytest.raises(HTTPException) as error:
        await service.download_authorized(db_session, source=source, file=foreign_file)

    assert error.value.status_code == 403
    assert gateway.download_calls == []


@pytest.mark.asyncio
async def test_active_source_downloads_only_after_scope_authorization(db_session, tmp_path) -> None:
    organization = Organization(name="Knowledge authorized download")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    connector_service = await _drive_connector_service(db_session, organization, tmp_path)
    gateway = FakeDriveGateway()
    service = KnowledgeSourceService(connector_service, FakeDriveGatewayFactory(gateway))
    await _configuration_grant(db_session, principal, service)
    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id="approved-root",
    )
    authorized_file = DriveFile(
        id="inside-file",
        name="shared.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        modified_time=datetime.now(UTC),
        parent_ids=("approved-root-child",),
        web_view_link=None,
        removed=False,
    )

    content = await service.download_authorized(db_session, source=source, file=authorized_file)

    assert content == b"authorized content"
    assert gateway.download_calls == ["inside-file"]
