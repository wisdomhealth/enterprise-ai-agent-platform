import pytest

from app.modules.connectors.models import ConnectorSecret
from app.modules.connectors.service import ConnectorService
from app.modules.identity.models import Organization


@pytest.mark.asyncio
async def test_database_never_contains_plain_refresh_token(db_session, tmp_path) -> None:
    organization = Organization(name="Connector encryption test")
    db_session.add(organization)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"b" * 32)
    service = ConnectorService.for_file_key(key_path, app_env="development")

    connector = await service.create_drive_connector(
        db_session,
        organization_id=organization.id,
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
