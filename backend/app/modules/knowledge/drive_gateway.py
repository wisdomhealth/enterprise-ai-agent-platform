from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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
    def list_changes(
        self, sync_cursor: str | None
    ) -> Awaitable[tuple[list[DriveFile], str | None]]: ...

    def get(self, file_id: str) -> Awaitable[DriveFile | None]: ...

    def download(self, file_id: str) -> Awaitable[bytes]: ...

    def resolve_descendant_folder_ids(self, root_folder_id: str) -> Awaitable[set[str]]: ...


class DriveGateway:
    """Narrow gateway for a Google Drive credential with readonly OAuth scope only."""

    oauth_scopes = ("https://www.googleapis.com/auth/drive.readonly",)

    def __init__(self, client: DriveReadClient | None = None) -> None:
        self._client = client

    def _require_client(self) -> DriveReadClient:
        if self._client is None:
            raise RuntimeError("Google Drive read-only client is not configured")
        return self._client

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
