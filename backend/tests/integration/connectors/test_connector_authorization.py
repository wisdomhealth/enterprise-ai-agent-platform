from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.audit.models import AuditEvent
from app.modules.connectors.models import ConnectorKind
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, UserRole
from app.modules.outbox.models import OutboxEvent


def _principal(role: UserRole) -> Principal:
    return Principal(
        subject_id=uuid4(),
        organization_id=uuid4(),
        email="operator@example.test",
        role=role,
        session_id=uuid4(),
        csrf_hash="csrf",
    )


def test_only_administrators_can_manage_connectors(tmp_path) -> None:
    service = ConnectorService.for_file_key(
        tmp_path / "connector-master-key", app_env="development"
    )

    with pytest.raises(HTTPException) as error:
        service.require_admin(_principal(UserRole.MEMBER))

    assert error.value.status_code == 403
    service.require_admin(_principal(UserRole.ADMIN))


@pytest.mark.asyncio
async def test_admin_authorization_emits_audit_and_outbox(db_session, tmp_path) -> None:
    organization = Organization(name="Connector audit test")
    db_session.add(organization)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"c" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")
    principal = Principal(
        subject_id=uuid4(),
        organization_id=organization.id,
        email="admin@example.test",
        role=UserRole.ADMIN,
        session_id=uuid4(),
        csrf_hash="csrf",
    )

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
