import asyncio
import base64
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses
from typing import Any, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # type: ignore[import-untyped]

from app.core.config import Settings

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SCOPES = (GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE)


class GmailAuthorizationError(RuntimeError):
    """The credential is revoked or does not grant the approved Gmail scopes."""


class GmailDefinitiveDeliveryError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class GmailAmbiguousDeliveryError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class GmailMessage:
    id: str
    thread_id: str
    history_id: str | None
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body: str
    received_at: datetime
    raw_content_ref: str


@dataclass(frozen=True, slots=True)
class GmailHistoryPage:
    message_ids: tuple[str, ...]
    history_id: str
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class GmailSendResult:
    gmail_message_id: str
    gmail_thread_id: str


@dataclass(frozen=True, slots=True)
class GmailSentMessage:
    gmail_message_id: str
    gmail_thread_id: str
    deterministic_message_id: str


class GmailReadClient(Protocol):
    def list_history(
        self, start_history_id: str | None, page_token: str | None = None
    ) -> Awaitable[GmailHistoryPage]: ...

    def get_message(self, message_id: str) -> Awaitable[GmailMessage]: ...

    def send_raw(self, raw_message: bytes, *, thread_id: str) -> Awaitable[GmailSendResult]: ...

    def find_sent(
        self,
        *,
        deterministic_message_id: str,
        thread_id: str,
        recipients: tuple[str, ...],
        sent_after: datetime,
        sent_before: datetime,
    ) -> Awaitable[GmailSentMessage | None]: ...


class GmailGateway:
    """Narrow Gmail ingestion and approved-delivery surface."""

    oauth_scopes = GMAIL_SCOPES

    def __init__(self, client: GmailReadClient | None = None) -> None:
        self._client = client

    def _require_client(self) -> GmailReadClient:
        if self._client is None:
            raise RuntimeError("Gmail client is not configured")
        return self._client

    async def list_history(
        self, start_history_id: str | None, page_token: str | None = None
    ) -> GmailHistoryPage:
        return await self._require_client().list_history(start_history_id, page_token)

    async def get_message(self, message_id: str) -> GmailMessage:
        return await self._require_client().get_message(message_id)

    async def send_raw(self, raw_message: bytes, *, thread_id: str) -> GmailSendResult:
        return await self._require_client().send_raw(raw_message, thread_id=thread_id)

    async def find_sent(
        self,
        *,
        deterministic_message_id: str,
        thread_id: str,
        recipients: tuple[str, ...],
        sent_after: datetime,
        sent_before: datetime,
    ) -> GmailSentMessage | None:
        return await self._require_client().find_sent(
            deterministic_message_id=deterministic_message_id,
            thread_id=thread_id,
            recipients=recipients,
            sent_after=sent_after,
            sent_before=sent_before,
        )


@dataclass(frozen=True, slots=True)
class GmailConnection:
    gateway: GmailGateway


class GmailGatewayFactory(Protocol):
    def create(self, *, refresh_token: str) -> Awaitable[GmailConnection]: ...


class GoogleGmailReadClient:
    def __init__(self, gmail_api: Any) -> None:
        self._gmail_api = gmail_api

    async def list_history(
        self, start_history_id: str | None, page_token: str | None = None
    ) -> GmailHistoryPage:
        try:
            if start_history_id is None or _page_token_kind(page_token) == "bootstrap":
                return await self._bootstrap_page(start_history_id, page_token)
            response = await asyncio.to_thread(
                lambda: (
                    self._gmail_api.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        labelId="INBOX",
                        pageToken=_provider_page_token(page_token, "history"),
                    )
                    .execute()
                )
            )
            message_ids: list[str] = []
            for history in response.get("history", []):
                if not isinstance(history, Mapping):
                    continue
                for added in history.get("messagesAdded", []):
                    if not isinstance(added, Mapping):
                        continue
                    message = added.get("message")
                    if isinstance(message, Mapping) and isinstance(message.get("id"), str):
                        message_ids.append(message["id"])
            history_id = response.get("historyId")
            if not isinstance(history_id, str) or not history_id:
                raise RuntimeError("Gmail history response omitted historyId")
            raw_next = response.get("nextPageToken")
            next_page_token = (
                f"history:{raw_next}" if isinstance(raw_next, str) and raw_next else None
            )
            return GmailHistoryPage(tuple(dict.fromkeys(message_ids)), history_id, next_page_token)
        except Exception as error:
            _raise_authorization_error(error)
            raise

    async def _bootstrap_page(
        self, history_anchor: str | None, page_token: str | None
    ) -> GmailHistoryPage:
        response_task = asyncio.to_thread(
            lambda: (
                self._gmail_api.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=["INBOX"],
                    maxResults=100,
                    pageToken=_provider_page_token(page_token, "bootstrap"),
                )
                .execute()
            )
        )
        if history_anchor is None:
            profile = await asyncio.to_thread(
                lambda: self._gmail_api.users().getProfile(userId="me").execute()
            )
            history_id = profile.get("historyId")
            if not isinstance(history_id, str) or not history_id:
                raise RuntimeError("Gmail profile omitted historyId")
            response = await response_task
        else:
            history_id = history_anchor
            response = await response_task
        message_ids = tuple(
            item["id"]
            for item in response.get("messages", [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        )
        raw_next = response.get("nextPageToken")
        next_page_token = (
            f"bootstrap:{raw_next}" if isinstance(raw_next, str) and raw_next else None
        )
        return GmailHistoryPage(message_ids, history_id, next_page_token)

    async def get_message(self, message_id: str) -> GmailMessage:
        try:
            response = await asyncio.to_thread(
                lambda: (
                    self._gmail_api.users()
                    .messages()
                    .get(userId="me", id=message_id, format="full")
                    .execute()
                )
            )
            return _message_from_payload(response)
        except Exception as error:
            _raise_authorization_error(error)
            raise

    async def send_raw(self, raw_message: bytes, *, thread_id: str) -> GmailSendResult:
        raw = base64.urlsafe_b64encode(raw_message).decode().rstrip("=")
        try:
            response = await asyncio.to_thread(
                lambda: (
                    self._gmail_api.users()
                    .messages()
                    .send(userId="me", body={"raw": raw, "threadId": thread_id})
                    .execute()
                )
            )
        except Exception as error:
            _raise_delivery_error(error)
            raise
        message_id = response.get("id")
        result_thread_id = response.get("threadId")
        if not isinstance(message_id, str) or not message_id:
            raise GmailAmbiguousDeliveryError("GMAIL_RESPONSE_MISSING_MESSAGE_ID")
        if not isinstance(result_thread_id, str) or not result_thread_id:
            raise GmailAmbiguousDeliveryError("GMAIL_RESPONSE_MISSING_THREAD_ID")
        return GmailSendResult(message_id, result_thread_id)

    async def find_sent(
        self,
        *,
        deterministic_message_id: str,
        thread_id: str,
        recipients: tuple[str, ...],
        sent_after: datetime,
        sent_before: datetime,
    ) -> GmailSentMessage | None:
        query = (
            f"in:sent rfc822msgid:{deterministic_message_id} "
            f"after:{int(sent_after.timestamp())} before:{int(sent_before.timestamp())}"
        )
        try:
            response = await asyncio.to_thread(
                lambda: (
                    self._gmail_api.users()
                    .messages()
                    .list(userId="me", q=query, maxResults=10)
                    .execute()
                )
            )
            expected_recipients = {_address_only(value) for value in recipients}
            for candidate in response.get("messages", []):
                if not isinstance(candidate, Mapping) or not isinstance(candidate.get("id"), str):
                    continue
                candidate_id = candidate["id"]

                def load_metadata() -> Any:
                    return (
                        self._gmail_api.users()
                        .messages()
                        .get(
                            userId="me",
                            id=candidate_id,
                            format="metadata",
                            metadataHeaders=["Message-ID", "To", "Cc"],
                        )
                        .execute()
                    )

                metadata = await asyncio.to_thread(load_metadata)
                headers = _headers(metadata)
                actual_recipients = {
                    address.lower()
                    for _name, address in getaddresses(
                        [headers.get("to", ""), headers.get("cc", "")]
                    )
                    if address
                }
                internal_date = metadata.get("internalDate")
                try:
                    sent_at = datetime.fromtimestamp(int(internal_date) / 1_000, tz=UTC)
                except (TypeError, ValueError, OSError):
                    continue
                if (
                    headers.get("message-id") == deterministic_message_id
                    and metadata.get("threadId") == thread_id
                    and expected_recipients == actual_recipients
                    and sent_after <= sent_at <= sent_before
                ):
                    return GmailSentMessage(
                        gmail_message_id=candidate["id"],
                        gmail_thread_id=thread_id,
                        deterministic_message_id=deterministic_message_id,
                    )
            return None
        except Exception as error:
            _raise_authorization_error(error)
            raise


class GoogleGmailGatewayFactory:
    """Constructs Gmail clients with only the separately approved read/send scopes."""

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleGmailGatewayFactory | None":
        if settings.google_gmail_client_id is None or settings.google_gmail_client_secret is None:
            return None
        return cls(
            client_id=settings.google_gmail_client_id.get_secret_value(),
            client_secret=settings.google_gmail_client_secret.get_secret_value(),
        )

    async def create(self, *, refresh_token: str) -> GmailConnection:
        credentials = Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=list(GMAIL_SCOPES),
        )
        api = await asyncio.to_thread(
            lambda: build("gmail", "v1", credentials=credentials, cache_discovery=False)
        )
        return GmailConnection(GmailGateway(GoogleGmailReadClient(api)))


def _provider_page_token(page_token: str | None, expected_kind: str) -> str | None:
    if page_token is None:
        return None
    kind, separator, token = page_token.partition(":")
    if not separator or kind != expected_kind or not token:
        raise ValueError("Gmail pagination token does not match the active cursor")
    return token


def _page_token_kind(page_token: str | None) -> str | None:
    if page_token is None:
        return None
    kind, separator, token = page_token.partition(":")
    if not separator or kind not in {"bootstrap", "history"} or not token:
        raise ValueError("invalid Gmail pagination token")
    return kind


def _message_from_payload(payload: Mapping[str, Any]) -> GmailMessage:
    message_id = payload.get("id")
    thread_id = payload.get("threadId")
    if not isinstance(message_id, str) or not message_id:
        raise RuntimeError("Gmail message omitted id")
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("Gmail message omitted threadId")
    message_payload = payload.get("payload")
    if not isinstance(message_payload, Mapping):
        raise RuntimeError("Gmail message omitted MIME payload")
    headers = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in message_payload.get("headers", [])
        if isinstance(item, Mapping)
    }
    sender = headers.get("from", "")
    recipients = tuple(
        _format_address(name, address)
        for name, address in getaddresses([headers.get("to", ""), headers.get("cc", "")])
        if address
    )
    internal_date = payload.get("internalDate")
    if not isinstance(internal_date, (str, int)):
        raise RuntimeError("Gmail message omitted a valid internalDate")
    try:
        received_at = datetime.fromtimestamp(int(internal_date) / 1_000, tz=UTC)
    except (TypeError, ValueError, OSError) as error:
        raise RuntimeError("Gmail message omitted a valid internalDate") from error
    history_id = payload.get("historyId")
    return GmailMessage(
        id=message_id,
        thread_id=thread_id,
        history_id=history_id if isinstance(history_id, str) else None,
        sender=sender,
        recipients=recipients,
        subject=headers.get("subject", ""),
        body=_plain_text_body(message_payload),
        received_at=received_at,
        raw_content_ref=f"gmail://users/me/messages/{message_id}",
    )


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    body = payload.get("payload")
    if not isinstance(body, Mapping):
        return {}
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in body.get("headers", [])
        if isinstance(item, Mapping)
    }


def _address_only(value: str) -> str:
    parsed = getaddresses([value])
    return parsed[0][1].lower() if parsed else value.strip().lower()


def _plain_text_body(part: Mapping[str, Any]) -> str:
    mime_type = part.get("mimeType")
    body = part.get("body")
    if mime_type == "text/plain" and isinstance(body, Mapping):
        data = body.get("data")
        if isinstance(data, str):
            return _decode_base64url(data)
    for child in part.get("parts", []):
        if isinstance(child, Mapping):
            text = _plain_text_body(child)
            if text:
                return text
    if isinstance(body, Mapping) and isinstance(body.get("data"), str):
        raw = _decode_base64url(body["data"])
        if mime_type == "text/html":
            return re.sub(r"<[^>]+>", " ", raw)
        return raw
    return ""


def _decode_base64url(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")


def _format_address(name: str, address: str) -> str:
    normalized = address.strip().lower()
    return f"{name.strip()} <{normalized}>" if name.strip() else normalized


def _raise_authorization_error(error: Exception) -> None:
    status_code = getattr(getattr(error, "resp", None), "status", None)
    if status_code is None:
        status_code = getattr(error, "status_code", None)
    if status_code in (401, 403) or error.__class__.__name__ in {
        "RefreshError",
        "InvalidGrantError",
    }:
        raise GmailAuthorizationError("Gmail authorization is no longer valid") from error


def _raise_delivery_error(error: Exception) -> None:
    try:
        _raise_authorization_error(error)
    except GmailAuthorizationError as authorization_error:
        raise GmailDefinitiveDeliveryError("GMAIL_AUTHORIZATION_FAILED") from authorization_error
    status_code = getattr(getattr(error, "resp", None), "status", None)
    if status_code is None:
        status_code = getattr(error, "status_code", None)
    if status_code in {400, 404, 429}:
        raise GmailDefinitiveDeliveryError(f"GMAIL_HTTP_{status_code}") from error
    raise GmailAmbiguousDeliveryError("GMAIL_RESPONSE_AMBIGUOUS") from error
