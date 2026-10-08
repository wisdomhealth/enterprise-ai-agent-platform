import json

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from httplib2 import Response

from app.core.config import Settings
from app.main import create_app
from app.modules.knowledge import drive_gateway
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
    ("change", "expected_removed", "expected_trashed"),
    (
        (
            {"fileId": "trashed", "removed": False, "file": {"id": "trashed", "trashed": True}},
            False,
            True,
        ),
        ({"fileId": "deleted", "removed": True}, True, False),
        (
            {
                "fileId": "active",
                "removed": False,
                "file": {"id": "active", "name": "active.pdf", "trashed": False},
            },
            False,
            False,
        ),
    ),
)
async def test_change_list_treats_trash_and_removal_as_deletion(
    change: dict[str, object], expected_removed: bool, expected_trashed: bool
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
    assert files[0].trashed is expected_trashed
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


@pytest.mark.asyncio
async def test_change_list_consumes_every_page_before_returning_changes() -> None:
    requested_tokens: list[str] = []
    responses = {
        "cursor": {
            "changes": [{"fileId": "file-a", "removed": True}],
            "nextPageToken": "page-2",
        },
        "page-2": {
            "changes": [
                {
                    "fileId": "file-a",
                    "removed": False,
                    "file": {
                        "id": "file-a",
                        "name": "file-a.pdf",
                        "mimeType": "application/pdf",
                        "parents": ["root"],
                    },
                }
            ],
            "newStartPageToken": "cursor-2",
        },
    }

    class FakeRequest:
        def __init__(self, token: str) -> None:
            self.token = token

        def execute(self) -> dict[str, object]:
            return responses[self.token]

    class FakeChanges:
        def list(self, **kwargs: object) -> FakeRequest:
            token = str(kwargs["pageToken"])
            requested_tokens.append(token)
            return FakeRequest(token)

    class FakeDriveApi:
        def changes(self) -> FakeChanges:
            return FakeChanges()

    files, cursor = await GoogleDriveReadClient(FakeDriveApi()).list_changes("cursor")

    assert requested_tokens == ["cursor", "page-2"]
    assert cursor == "cursor-2"
    assert [(item.id, item.removed) for item in files] == [("file-a", False)]


@pytest.mark.asyncio
async def test_change_list_requires_final_start_page_token() -> None:
    class FakeRequest:
        def execute(self) -> dict[str, object]:
            return {"changes": []}

    class FakeChanges:
        def list(self, **_kwargs: object) -> FakeRequest:
            return FakeRequest()

    class FakeDriveApi:
        def changes(self) -> FakeChanges:
            return FakeChanges()

    with pytest.raises(RuntimeError, match="final start-page token"):
        await GoogleDriveReadClient(FakeDriveApi()).list_changes("cursor")


def _http_error(status: int, reason: str) -> HttpError:
    response = Response({"status": str(status)})
    content = json.dumps(
        {"error": {"errors": [{"reason": reason}], "code": status, "message": reason}}
    ).encode()
    return HttpError(response, content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason", "expected_reason"),
    (
        (404, "notFound", "NOT_FOUND_OR_NO_ACCESS"),
        (403, "insufficientFilePermissions", "ACCESS_DENIED"),
    ),
)
async def test_specific_file_unavailability_keeps_accurate_evidence(
    status: int, reason: str, expected_reason: str
) -> None:
    class FakeRequest:
        def execute(self) -> dict[str, object]:
            raise _http_error(status, reason)

    class FakeFiles:
        def get(self, **_kwargs: object) -> FakeRequest:
            return FakeRequest()

    class FakeDriveApi:
        def files(self) -> FakeFiles:
            return FakeFiles()

    with pytest.raises(Exception) as caught:
        await GoogleDriveReadClient(FakeDriveApi()).get("target-file")

    assert caught.value.__class__.__name__ == "DriveFileUnavailable"
    assert getattr(caught.value, "reason").value == expected_reason


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (403, "rateLimitExceeded"),
        (403, "userRateLimitExceeded"),
        (403, "quotaExceeded"),
        (500, "backendError"),
        (503, "backendError"),
    ),
)
def test_request_failures_are_not_file_unavailability(status: int, reason: str) -> None:
    error = _http_error(status, reason)

    assert drive_gateway.drive_file_unavailability_reason(error) is None
    assert drive_gateway.is_drive_authorization_error(error) is False


def test_oauth_failure_is_not_file_unavailability() -> None:
    error = _http_error(401, "authError")

    assert drive_gateway.drive_file_unavailability_reason(error) is None
    assert drive_gateway.is_drive_authorization_error(error) is True


def test_invalid_grant_refresh_failure_requires_reauthorization() -> None:
    error = RefreshError("invalid_grant", {"error": "invalid_grant"})

    assert drive_gateway.is_drive_authorization_error(error) is True


def test_retryable_refresh_server_error_is_not_authorization_loss() -> None:
    error = RefreshError("server_error", {"error": "server_error"}, retryable=True)

    assert drive_gateway.is_drive_authorization_error(error) is False
