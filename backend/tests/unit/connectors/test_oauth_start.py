from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.modules.connectors.models import ConnectorKind
from app.modules.connectors.router import _connector_service
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session
from app.modules.identity.models import UserRole


class ConnectorServiceProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def require_authorization_start(
        self,
        _db_session: object,
        *,
        principal: Principal,
        kind: ConnectorKind,
    ) -> None:
        assert principal.role is UserRole.ADMIN
        assert kind is ConnectorKind.DRIVE
        self.calls += 1


def _admin() -> Principal:
    return Principal(
        subject_id=uuid4(),
        organization_id=uuid4(),
        email="admin@example.test",
        role=UserRole.ADMIN,
        session_id=uuid4(),
        csrf_hash="csrf",
    )


def _app(probe: ConnectorServiceProbe) -> FastAPI:
    app = create_app(
        Settings.model_validate(
            {
                "SESSION_SECRET": "test-session-secret",
                "PUBLIC_BASE_URL": "http://testserver",
                "GOOGLE_DRIVE_CLIENT_ID": "test-client-id",
                "GOOGLE_DRIVE_CLIENT_SECRET": "test-client-secret",
            }
        )
    )

    async def db_session() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[require_staff_session] = _admin
    app.dependency_overrides[get_db_session] = db_session
    app.dependency_overrides[_connector_service] = lambda: probe
    return app


def test_same_origin_authorize_starts_oauth_flow() -> None:
    probe = ConnectorServiceProbe()
    with TestClient(_app(probe)) as client:
        response = client.get(
            "/api/v1/admin/connectors/DRIVE/authorize",
            headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert probe.calls == 1


def test_cross_site_authorize_is_rejected_before_starting_flow() -> None:
    probe = ConnectorServiceProbe()
    with TestClient(_app(probe)) as client:
        response = client.get(
            "/api/v1/admin/connectors/DRIVE/authorize",
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://attacker.example"},
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert probe.calls == 0
