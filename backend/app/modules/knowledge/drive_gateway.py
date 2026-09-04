import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # type: ignore[import-untyped]

from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: datetime | None
    parent_ids: tuple[str, ...]
    web_view_link: str | None
    removed: bool


class DriveReadClient(Protocol):
    def get_start_page_token(self) -> Awaitable[str]: ...

    def list_changes(
        self, sync_cursor: str | None
    ) -> Awaitable[tuple[list[DriveFile], str | None]]: ...

    def get(self, file_id: str) -> Awaitable[DriveFile | None]: ...

    def download(self, file_id: str) -> Awaitable[bytes]: ...

    def resolve_descendant_folder_ids(self, root_folder_id: str) -> Awaitable[set[str]]: ...


@dataclass(frozen=True, slots=True)
class DriveConnection:
    gateway: "DriveGateway"
    connection_identity: str


class DriveGatewayFactory(Protocol):
    """Creates a readonly Drive gateway from an encrypted connector credential."""

    def create(self, *, refresh_token: str) -> Awaitable[DriveConnection]: ...


class DriveGateway:
    """Narrow gateway for a Google Drive credential with readonly OAuth scope only."""

    oauth_scopes = ("https://www.googleapis.com/auth/drive.readonly",)

    def __init__(self, client: DriveReadClient | None = None) -> None:
        self._client = client

    def _require_client(self) -> DriveReadClient:
        if self._client is None:
            raise RuntimeError("Google Drive read-only client is not configured")
        return self._client

    async def get_start_page_token(self) -> str:
        return await self._require_client().get_start_page_token()

    async def list_changes(
        self, sync_cursor: str | None = None
    ) -> tuple[list[DriveFile], str | None]:
        return await self._require_client().list_changes(sync_cursor)

    async def get(self, file_id: str) -> DriveFile | None:
        return await self._require_client().get(file_id)

    async def download(self, file_id: str) -> bytes:
        return await self._require_client().download(file_id)

    async def resolve_descendant_folder_ids(self, root_folder_id: str) -> set[str]:
        return await self._require_client().resolve_descendant_folder_ids(root_folder_id)


class GoogleDriveReadClient:
    """Google Drive v3 adapter with no mutating API surface."""

    _FILE_FIELDS = "id,name,mimeType,modifiedTime,parents,webViewLink"

    def __init__(self, drive_api: Any) -> None:
        self._drive_api = drive_api

    async def connection_identity(self) -> str:
        response = await asyncio.to_thread(
            lambda: self._drive_api.about()
            .get(fields="user(emailAddress,displayName)")
            .execute()
        )
        user = response.get("user", {})
        identity = user.get("emailAddress") or user.get("displayName")
        if not isinstance(identity, str) or not identity:
            raise RuntimeError("Google Drive connection identity is unavailable")
        return identity

    async def get_start_page_token(self) -> str:
        response = await asyncio.to_thread(
            lambda: self._drive_api.changes().getStartPageToken().execute()
        )
        page_token = response.get("startPageToken")
        if not isinstance(page_token, str) or not page_token:
            raise RuntimeError("Google Drive start-page token is unavailable")
        return page_token

    async def list_changes(self, sync_cursor: str | None) -> tuple[list[DriveFile], str | None]:
        if sync_cursor is None:
            raise ValueError("Google Drive change listing requires a persisted sync cursor")
        response = await asyncio.to_thread(
            lambda: self._drive_api.changes()
            .list(
                pageToken=sync_cursor,
                fields=(
                    "nextPageToken,newStartPageToken,"
                    f"changes(fileId,removed,file({self._FILE_FIELDS}))"
                ),
            )
            .execute()
        )
        files = [
            self._drive_change_to_file(change)
            for change in response.get("changes", [])
            if isinstance(change, Mapping)
        ]
        next_cursor = response.get("newStartPageToken") or response.get("nextPageToken")
        return files, next_cursor if isinstance(next_cursor, str) else None

    async def get(self, file_id: str) -> DriveFile | None:
        response = await asyncio.to_thread(
            lambda: self._drive_api.files()
            .get(fileId=file_id, fields=self._FILE_FIELDS)
            .execute()
        )
        return self._drive_file_from_payload(response)

    async def download(self, file_id: str) -> bytes:
        content = await asyncio.to_thread(
            lambda: self._drive_api.files().get_media(fileId=file_id).execute()
        )
        if not isinstance(content, bytes):
            raise RuntimeError("Google Drive download returned a non-bytes payload")
        return content

    async def resolve_descendant_folder_ids(self, root_folder_id: str) -> set[str]:
        descendants: set[str] = set()
        pending = [root_folder_id]
        while pending:
            parent_id = pending.pop()
            page_token: str | None = None
            while True:
                response = await asyncio.to_thread(
                    lambda: self._drive_api.files()
                    .list(
                        q=(
                            f"'{self._drive_query_literal(parent_id)}' in parents and "
                            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                        ),
                        fields="nextPageToken,files(id)",
                        pageToken=page_token,
                    )
                    .execute()
                )
                for item in response.get("files", []):
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                        folder_id = item["id"]
                        if folder_id not in descendants:
                            descendants.add(folder_id)
                            pending.append(folder_id)
                page_token = response.get("nextPageToken")
                if not isinstance(page_token, str):
                    break
        return descendants

    @classmethod
    def _drive_change_to_file(cls, change: Mapping[str, Any]) -> DriveFile:
        payload = change.get("file")
        if not isinstance(payload, Mapping):
            payload = {"id": change.get("fileId")}
        return cls._drive_file_from_payload(payload, removed=bool(change.get("removed", False)))

    @staticmethod
    def _drive_file_from_payload(
        payload: Mapping[str, Any], *, removed: bool = False
    ) -> DriveFile:
        file_id = payload.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise RuntimeError("Google Drive file response omitted its identifier")
        raw_modified_time = payload.get("modifiedTime")
        modified_time = (
            datetime.fromisoformat(raw_modified_time.replace("Z", "+00:00"))
            if isinstance(raw_modified_time, str)
            else None
        )
        if modified_time is not None and modified_time.tzinfo is None:
            modified_time = modified_time.replace(tzinfo=UTC)
        parents = payload.get("parents", [])
        raw_name = cast(object, payload.get("name"))
        raw_mime_type = cast(object, payload.get("mimeType"))
        raw_web_view_link = cast(object, payload.get("webViewLink"))
        return DriveFile(
            id=file_id,
            name=raw_name if isinstance(raw_name, str) else "",
            mime_type=raw_mime_type if isinstance(raw_mime_type, str) else "",
            modified_time=modified_time,
            parent_ids=tuple(parent for parent in parents if isinstance(parent, str)),
            web_view_link=raw_web_view_link if isinstance(raw_web_view_link, str) else None,
            removed=removed,
        )

    @staticmethod
    def _drive_query_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveGatewayFactory:
    """Constructs readonly Drive v3 clients from a Task 6 encrypted connector token."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        build_service: Callable[..., Any] = build,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._build_service = build_service

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleDriveGatewayFactory | None":
        if settings.google_drive_client_id is None or settings.google_drive_client_secret is None:
            return None
        return cls(
            client_id=settings.google_drive_client_id.get_secret_value(),
            client_secret=settings.google_drive_client_secret.get_secret_value(),
        )

    async def create(self, *, refresh_token: str) -> DriveConnection:
        credentials = Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=DriveGateway.oauth_scopes,
        )
        drive_api = self._build_service(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )
        client = GoogleDriveReadClient(drive_api)
        return DriveConnection(
            gateway=DriveGateway(client),
            connection_identity=await client.connection_identity(),
        )
