from uuid import uuid4

import pytest

from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import ConnectorKind, ConnectorSecret
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus


@pytest.mark.asyncio
async def test_database_never_contains_plain_refresh_token(db_session, tmp_path) -> None:
    organization = Organization(name="Connector encryption test")
    db_session.add(organization)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"b" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"connector-test-{uuid4()}",
        email="admin@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    principal = Principal(
        subject_id=user.id,
        organization_id=organization.id,
        email=user.email,
        role=user.role,
        session_id=uuid4(),
        csrf_hash="csrf",
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
        refresh_token="real-looking-but-test-only-token",
    )
    await db_session.commit()

    stored = await db_session.get(ConnectorSecret, connector.secret_id)
    assert stored is not None
    assert b"real-looking-but-test-only-token" not in stored.ciphertext
    assert (
        await service.load_refresh_token(db_session, connector)
        == "real-looking-but-test-only-token"
    )
