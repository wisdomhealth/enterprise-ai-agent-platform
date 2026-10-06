import pytest

from app.core.config import Settings
from app.main import create_app
from app.modules.knowledge.drive_gateway import (
    DriveGateway,
    GoogleDriveGatewayFactory,
    GoogleDriveReadClient,
)


def test_drive_gateway_declares_read_only_scope() -> None:
    assert DriveGateway.oauth_scopes == ("https://www.googleapis.com/auth/drive.readonly",)


def test_drive_gateway_exposes_no_mutating_drive_methods() -> None:
    gateway_methods = set(dir(DriveGateway))

    assert {"create", "update", "move", "delete"}.isdisjoint(gateway_methods)


@pytest.mark.asyncio
async def test_google_factory_builds_readonly_client_from_connector_refresh_token() -> None:
    captured: dict[str, object] = {}

    class FakeRequest:
        def execute(self) -> dict[str, object]:
            return {"user": {"emailAddress": "reader@example.test"}}

    class FakeAbout:
        def get(self, *, fields: str) -> FakeRequest:
            captured["about_fields"] = fields
            return FakeRequest()

    class FakeDriveApi:
        def about(self) -> FakeAbout:
            return FakeAbout()

    def build_service(*args: object, **kwargs: object) -> FakeDriveApi:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeDriveApi()

    factory = GoogleDriveGatewayFactory(
        client_id="client-id",
        client_secret="client-secret",
        build_service=build_service,
    )

    connection = await factory.create(refresh_token="connector-refresh-token")

    credentials = captured["kwargs"]["credentials"]  # type: ignore[index]
    assert captured["args"] == ("drive", "v3")
    assert credentials.refresh_token == "connector-refresh-token"  # type: ignore[union-attr]
    assert credentials.scopes == DriveGateway.oauth_scopes  # type: ignore[union-attr]
    assert connection.connection_identity == "reader@example.test"
    assert captured["about_fields"] == "user(emailAddress,displayName)"


def test_google_factory_requires_drive_oauth_client_configuration() -> None:
    assert GoogleDriveGatewayFactory.from_settings(Settings()) is None
    assert (
        GoogleDriveGatewayFactory.from_settings(
            Settings(
                GOOGLE_DRIVE_CLIENT_ID="client-id",
                GOOGLE_DRIVE_CLIENT_SECRET="client-secret",
            )
        )
        is not None
    )


def test_app_installs_google_drive_factory_when_oauth_client_is_configured() -> None:
    app = create_app(
        Settings(
            GOOGLE_DRIVE_CLIENT_ID="client-id",
            GOOGLE_DRIVE_CLIENT_SECRET="client-secret",
        )
    )

    assert isinstance(app.state.drive_gateway_factory, GoogleDriveGatewayFactory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected_removed"),
    (
        ({"fileId": "trashed", "removed": False, "file": {"id": "trashed", "trashed": True}}, True),
        ({"fileId": "deleted", "removed": True}, True),
        (
            {
                "fileId": "active",
                "removed": False,
                "file": {"id": "active", "name": "active.pdf", "trashed": False},
            },
            False,
        ),
    ),
)
async def test_change_list_treats_trash_and_removal_as_deletion(
    change: dict[str, object], expected_removed: bool
) -> None:
    captured: dict[str, object] = {}

    class FakeRequest:
        def execute(self) -> dict[str, object]:
            return {"changes": [change], "newStartPageToken": "next"}

    class FakeChanges:
        def list(self, **kwargs: object) -> FakeRequest:
            captured.update(kwargs)
            return FakeRequest()

    class FakeDriveApi:
        def changes(self) -> FakeChanges:
            return FakeChanges()

    files, cursor = await GoogleDriveReadClient(FakeDriveApi()).list_changes("cursor")

    assert cursor == "next"
    assert files[0].removed is expected_removed
    assert "trashed" in str(captured["fields"])


@pytest.mark.asyncio
async def test_change_list_does_not_turn_api_failure_into_deletion() -> None:
    class FakeRequest:
        def execute(self) -> dict[str, object]:
            raise RuntimeError("temporary Drive failure")

    class FakeChanges:
        def list(self, **_kwargs: object) -> FakeRequest:
            return FakeRequest()

    class FakeDriveApi:
        def changes(self) -> FakeChanges:
            return FakeChanges()

    with pytest.raises(RuntimeError, match="temporary Drive failure"):
        await GoogleDriveReadClient(FakeDriveApi()).list_changes("cursor")
