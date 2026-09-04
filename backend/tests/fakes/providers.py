"""Deterministic in-process HTTP providers for cross-system release journeys.

The fakes deliberately preserve provider protocol boundaries.  Production
adapters still serialize HTTP requests and parse HTTP responses; only the
network transport is replaced with an in-process ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx


@dataclass(slots=True)
class DriveFixture:
    file_id: str
    name: str
    mime_type: str
    body: bytes
    parent_ids: tuple[str, ...] = ("approved-root",)
    modified_time: datetime = field(default_factory=lambda: datetime(2026, 9, 3, tzinfo=UTC))
    authorized: bool = True
    removed: bool = False


@dataclass(slots=True)
class GmailFixture:
    message_id: str
    thread_id: str
    raw: bytes
    history_id: str


@dataclass(slots=True)
class ProviderCall:
    provider: str
    method: str
    path: str
    body_sha256: str


@dataclass(slots=True)
class DeterministicProviderState:
    drive_pages: dict[str, tuple[list[str], str | None]] = field(default_factory=dict)
    drive_files: dict[str, DriveFixture] = field(default_factory=dict)
    gmail_history: dict[str, tuple[list[str], str | None]] = field(default_factory=dict)
    gmail_messages: dict[str, GmailFixture] = field(default_factory=dict)
    gmail_sent: dict[str, GmailFixture] = field(default_factory=dict)
    anthropic_answers: deque[dict[str, object]] = field(default_factory=deque)
    fail_next: dict[str, str] = field(default_factory=dict)
    calls: list[ProviderCall] = field(default_factory=list)
    start_page_token: str = "drive-start-1"


class DeterministicProviderStack:
    """Local fake Drive, Gmail, Anthropic and OpenAI HTTP endpoints."""

    def __init__(self) -> None:
        self.state = DeterministicProviderState()
        self._transport = httpx.MockTransport(self._dispatch)

    def client(self, provider: str) -> httpx.AsyncClient:
        if provider not in {"drive", "gmail", "anthropic", "openai"}:
            raise ValueError(f"unsupported fake provider: {provider}")
        return httpx.AsyncClient(
            transport=self._transport,
            base_url=f"https://{provider}.provider.test",
            headers={"X-Fake-Provider": provider},
        )

    def add_drive_file(self, fixture: DriveFixture) -> None:
        self.state.drive_files[fixture.file_id] = fixture

    def revoke_drive_file(self, file_id: str) -> None:
        fixture = self.state.drive_files[file_id]
        fixture.authorized = False
        fixture.removed = True

    def queue_anthropic_answer(
        self, *, text: str, claims: list[dict[str, object]] | None = None
    ) -> None:
        self.state.anthropic_answers.append({"text": text, "claims": claims or []})

    def fail_once(self, provider: str, error_code: str) -> None:
        self.state.fail_next[provider] = error_code

    def call_count(self, provider: str, path: str | None = None) -> int:
        return sum(
            call.provider == provider and (path is None or call.path == path)
            for call in self.state.calls
        )

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        provider = request.headers.get("X-Fake-Provider", request.url.host.split(".", 1)[0])
        body = request.content
        self.state.calls.append(
            ProviderCall(
                provider=provider,
                method=request.method,
                path=request.url.path,
                body_sha256=hashlib.sha256(body).hexdigest(),
            )
        )
        error = self.state.fail_next.pop(provider, None)
        if error == "timeout":
            raise httpx.ReadTimeout("deterministic provider timeout", request=request)
        if error is not None:
            return httpx.Response(503, json={"error": {"code": error}})
        handlers = {
            "drive": self._drive,
            "gmail": self._gmail,
            "anthropic": self._anthropic,
            "openai": self._openai,
        }
        return handlers[provider](request)

    def _drive(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": self.state.start_page_token})
        if request.url.path == "/drive/v3/changes":
            token = request.url.params.get("pageToken", "")
            ids, next_token = self.state.drive_pages.get(token, ([], None))
            changes = []
            for file_id in ids:
                fixture = self.state.drive_files[file_id]
                changes.append(
                    {
                        "fileId": file_id,
                        "removed": fixture.removed,
                        "file": {
                            "id": fixture.file_id,
                            "name": fixture.name,
                            "mimeType": fixture.mime_type,
                            "modifiedTime": fixture.modified_time.isoformat().replace(
                                "+00:00", "Z"
                            ),
                            "parents": list(fixture.parent_ids),
                        },
                    }
                )
            payload: dict[str, object] = {"changes": changes, "newStartPageToken": token}
            if next_token is not None:
                payload["nextPageToken"] = next_token
                payload.pop("newStartPageToken", None)
            return httpx.Response(200, json=payload)
        prefix = "/drive/v3/files/"
        if request.url.path.startswith(prefix):
            file_id = request.url.path.removeprefix(prefix)
            fixture = self.state.drive_files.get(file_id)
            if fixture is None:
                return httpx.Response(404, json={"error": {"code": "notFound"}})
            if not fixture.authorized:
                return httpx.Response(403, json={"error": {"code": "insufficientFilePermissions"}})
            if request.url.params.get("alt") == "media":
                return httpx.Response(200, content=fixture.body)
            return httpx.Response(
                200,
                json={"id": fixture.file_id, "name": fixture.name, "mimeType": fixture.mime_type},
            )
        return httpx.Response(404)

    def _gmail(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history"):
            token = request.url.params.get("startHistoryId", "")
            ids, next_token = self.state.gmail_history.get(token, ([], None))
            payload: dict[str, object] = {
                "history": [
                    {
                        "id": self.state.gmail_messages[item].history_id,
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": item,
                                    "threadId": self.state.gmail_messages[item].thread_id,
                                }
                            }
                        ],
                    }
                    for item in ids
                ]
            }
            if next_token is not None:
                payload["nextPageToken"] = next_token
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("/messages/send"):
            payload = json.loads(request.content or b"{}")
            raw = str(payload.get("raw", "")).encode()
            identity = hashlib.sha256(raw).hexdigest()[:24]
            existing = self.state.gmail_sent.get(identity)
            if existing is None:
                existing = GmailFixture(
                    message_id=f"sent-{identity}",
                    thread_id=str(payload.get("threadId", "thread-fake")),
                    raw=raw,
                    history_id="sent-history",
                )
                self.state.gmail_sent[identity] = existing
            if request.headers.get("X-Fake-Send-Then-Timeout") == "1":
                raise httpx.ReadTimeout("sent before timeout", request=request)
            return httpx.Response(
                200, json={"id": existing.message_id, "threadId": existing.thread_id}
            )
        if request.url.path.endswith("/messages"):
            query = request.url.params.get("q", "")
            matches = [
                {"id": message.message_id, "threadId": message.thread_id}
                for key, message in sorted(self.state.gmail_sent.items())
                if not query or key in query or message.message_id in query
            ]
            return httpx.Response(200, json={"messages": matches})
        return httpx.Response(404)

    def _anthropic(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/messages":
            return httpx.Response(404)
        answer = (
            self.state.anthropic_answers.popleft()
            if self.state.anthropic_answers
            else {
                "text": "I don't know based on the available information.",
                "claims": [],
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": "claude-fake-v1",
                "content": [{"type": "text", "text": json.dumps(answer, sort_keys=True)}],
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        )

    def _openai(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/embeddings":
            return httpx.Response(404)
        payload = json.loads(request.content)
        inputs = payload.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        vectors = [_stable_vector(str(value)) for value in inputs]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-fake-v1",
                "data": [
                    {"object": "embedding", "index": index, "embedding": vector}
                    for index, vector in enumerate(vectors)
                ],
                "usage": {"prompt_tokens": len(vectors), "total_tokens": len(vectors)},
            },
        )


def _stable_vector(value: str, dimensions: int = 1536) -> list[float]:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(dimensions)]


class HttpEmbeddingProvider:
    """Embedding protocol adapter used by E2E tests through the fake HTTP service."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post(
            "/v1/embeddings", json={"model": "text-embedding-fake-v1", "input": texts}
        )
        response.raise_for_status()
        data = response.json()["data"]
        return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]


class HttpDriveChangeBoundary:
    """Task 9 page-boundary adapter backed by the deterministic HTTP fake."""

    def __init__(self, providers: DeterministicProviderStack) -> None:
        self._providers = providers

    async def get_start_page_token(self, _db_session: object, *, source: object) -> str:
        del source
        async with self._providers.client("drive") as drive:
            response = await drive.get("/drive/v3/changes/startPageToken")
        response.raise_for_status()
        return str(response.json()["startPageToken"])

    async def list_changes(
        self, _db_session: object, *, source: object, sync_cursor: str
    ) -> tuple[list[object], str | None]:
        del source
        from app.modules.knowledge.drive_gateway import DriveFile

        async with self._providers.client("drive") as drive:
            response = await drive.get("/drive/v3/changes", params={"pageToken": sync_cursor})
        response.raise_for_status()
        payload = response.json()
        files = []
        for change in payload["changes"]:
            raw = change["file"]
            files.append(
                DriveFile(
                    id=change["fileId"],
                    name=raw.get("name", ""),
                    mime_type=raw.get("mimeType", ""),
                    modified_time=datetime.fromisoformat(
                        raw["modifiedTime"].replace("Z", "+00:00")
                    ),
                    parent_ids=tuple(raw.get("parents", [])),
                    web_view_link=None,
                    removed=bool(change["removed"]),
                )
            )
        return files, payload.get("nextPageToken") or payload.get("newStartPageToken")


class HttpStructuredAnswerProvider:
    """Generation protocol adapter that keeps Task 11 structured parsing semantics."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def generate(self, prompt: object) -> object:
        from app.modules.rag.llm import GeneratedAnswer, ProviderResponseError

        response = await self._client.post(
            "/v1/messages",
            json={"model": "claude-fake-v1", "prompt": str(prompt)},
        )
        response.raise_for_status()
        payload = response.json()
        try:
            content = json.loads(payload["content"][0]["text"])
            usage = payload["usage"]
            return GeneratedAnswer(
                text=content["text"],
                claims=content["claims"],
                model=payload["model"],
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
            )
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ProviderResponseError(
                "fake provider returned malformed structured data"
            ) from error


class HttpGmailDeliveryGateway:
    """Delivery protocol adapter with explicit ambiguous-outcome translation."""

    def __init__(self, client: httpx.AsyncClient, *, send_then_timeout: bool = False) -> None:
        self._client = client
        self._send_then_timeout = send_then_timeout

    async def send_raw(self, raw_message: bytes, *, thread_id: str) -> object:
        from app.modules.email.gmail_gateway import (
            GmailAmbiguousDeliveryError,
            GmailSendResult,
        )

        headers = {"X-Fake-Send-Then-Timeout": "1"} if self._send_then_timeout else {}
        try:
            response = await self._client.post(
                "/gmail/v1/users/me/messages/send",
                json={"raw": raw_message.decode("ascii"), "threadId": thread_id},
                headers=headers,
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise GmailAmbiguousDeliveryError("GMAIL_RESPONSE_TIMEOUT") from error
        payload = response.json()
        return GmailSendResult(payload["id"], payload["threadId"])
