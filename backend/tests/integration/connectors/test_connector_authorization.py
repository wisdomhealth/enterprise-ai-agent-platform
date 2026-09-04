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
from app.modules.outbox.models import OutboxEvent


async def _principal_for(
    db_session, organization: Organization, *, role: UserRole, email: str
) -> Principal:
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"connector-test-{uuid4()}",
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
async def test_admin_authorization_emits_audit_and_outbox(db_session, tmp_path) -> None:
    organization = Organization(name="Connector audit test")
    db_session.add(organization)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"c" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=principal.subject_id,
            resource_type="connector",
            resource_id=service.configuration_resource_id(organization.id, ConnectorKind.DRIVE),
            actions=["connector.create"],
        )
    )
    await db_session.flush()

    connector = await service.create_or_reauthorize(
        db_session,
        principal=principal,
        kind=ConnectorKind.DRIVE,
        refresh_token="test-only-refresh-token",
    )

    assert connector.organization_id == organization.id
    assert (
        await db_session.scalar(
            select(AuditEvent).where(
                AuditEvent.object_id == connector.id,
                AuditEvent.action == "connector.authorize",
            )
        )
        is not None
    )
    assert (
        await db_session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == connector.id,
                OutboxEvent.event_type == "connector.authorized",
            )
        )
        is not None
    )


@pytest.mark.asyncio
async def test_non_admin_cannot_create_connector_even_with_a_resource_grant(
    db_session, tmp_path
) -> None:
    organization = Organization(name="Connector member denial")
    db_session.add(organization)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"d" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")
    principal = await _principal_for(
        db_session, organization, role=UserRole.MEMBER, email="member@example.test"
    )
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=principal.subject_id,
            resource_type="connector",
            resource_id=service.configuration_resource_id(organization.id, ConnectorKind.DRIVE),
            actions=["connector.create"],
        )
    )
    await db_session.flush()

    with pytest.raises(HTTPException) as error:
        await service.create_or_reauthorize(
            db_session,
            principal=principal,
            kind=ConnectorKind.DRIVE,
            refresh_token="test-only-refresh-token",
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_reauthorize_requires_grant_for_connector_and_reauth_state(
    db_session, tmp_path
) -> None:
    organization = Organization(name="Connector reauthorization")
    db_session.add(organization)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"e" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    connector = Connector(
        organization_id=organization.id,
        kind=ConnectorKind.DRIVE,
        status=ConnectorStatus.REAUTH_REQUIRED,
        secret_id=(
            await service.store_refresh_token(
                db_session, organization_id=organization.id, refresh_token="old-token"
            )
        ).id,
    )
    db_session.add(connector)
    await db_session.flush()

    with pytest.raises(HTTPException) as no_grant:
        await service.create_or_reauthorize(
            db_session,
            principal=principal,
            kind=ConnectorKind.DRIVE,
            refresh_token="replacement-token",
        )
    assert no_grant.value.status_code == 403

    connector.status = ConnectorStatus.REAUTH_REQUIRED
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=principal.subject_id,
            resource_type="connector",
            resource_id=connector.id,
            actions=["connector.reauthorize"],
        )
    )
    await db_session.flush()
    reauthorized = await service.create_or_reauthorize(
        db_session,
        principal=principal,
        kind=ConnectorKind.DRIVE,
        refresh_token="replacement-token",
    )

    assert reauthorized.status is ConnectorStatus.ACTIVE
    with pytest.raises(HTTPException) as invalid_revoke:
        await service.create_or_reauthorize(
            db_session,
            principal=principal,
            kind=ConnectorKind.DRIVE,
            refresh_token="second-replacement-token",
        )
    assert invalid_revoke.value.status_code == 409


@pytest.mark.asyncio
async def test_revoke_hides_cross_organization_connector(db_session, tmp_path) -> None:
    first = Organization(name="Connector owner")
    second = Organization(name="Connector foreign")
    db_session.add_all([first, second])
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"f" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")
    foreign_principal = Principal(
        subject_id=uuid4(),
        organization_id=second.id,
        email="foreign-admin@example.test",
        role=UserRole.ADMIN,
        session_id=uuid4(),
        csrf_hash="csrf",
    )
    secret = await service.store_refresh_token(
        db_session, organization_id=first.id, refresh_token="owner-token"
    )
    connector = Connector(
        organization_id=first.id,
        kind=ConnectorKind.DRIVE,
        status=ConnectorStatus.ACTIVE,
        secret_id=secret.id,
    )
    db_session.add(connector)
    await db_session.flush()

    with pytest.raises(HTTPException) as error:
        await service.revoke(db_session, principal=foreign_principal, connector_id=connector.id)

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_revoke_requires_connector_grant(db_session, tmp_path) -> None:
    organization = Organization(name="Connector revoke grant")
    db_session.add(organization)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"g" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")
    principal = await _principal_for(
        db_session, organization, role=UserRole.ADMIN, email="admin@example.test"
    )
    secret = await service.store_refresh_token(
        db_session, organization_id=organization.id, refresh_token="owner-token"
    )
    connector = Connector(
        organization_id=organization.id,
        kind=ConnectorKind.DRIVE,
        status=ConnectorStatus.ACTIVE,
        secret_id=secret.id,
    )
    db_session.add(connector)
    await db_session.flush()

    with pytest.raises(HTTPException) as error:
        await service.revoke(db_session, principal=principal, connector_id=connector.id)

    assert error.value.status_code == 403
