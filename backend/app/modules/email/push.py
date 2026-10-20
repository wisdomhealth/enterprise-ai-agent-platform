import asyncio
import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.models import (
    EmailSyncState,
    GmailPushReceipt,
)
from app.modules.email.sync import GmailSyncCoordinator, GmailSyncPaused
from app.modules.knowledge.models import KnowledgeBase


class GmailPushValidationError(ValueError):
    pass


class GmailPushMailboxNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedGmailNotification:
    subscription_name: str
    pubsub_message_id: str
    published_at: datetime | None
    mailbox_address: str
    history_id: str


TokenVerifier = Callable[[str, str], Mapping[str, Any]]


def _google_token_verifier(token: str, audience: str) -> Mapping[str, Any]:
    return id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=audience)


class GmailPushAuthenticator:
    def __init__(
        self,
        *,
        audience: str,
        service_account_email: str,
        subscription_name: str,
        max_body_bytes: int,
        token_verifier: TokenVerifier = _google_token_verifier,
    ) -> None:
        self._audience = audience
        self._service_account_email = service_account_email.strip().lower()
        self._subscription_name = subscription_name
        self._max_body_bytes = max_body_bytes
        self._token_verifier = token_verifier

    async def verify(
        self, authorization: str | None, body: bytes
    ) -> VerifiedGmailNotification:
        if len(body) > self._max_body_bytes:
            raise GmailPushValidationError("GMAIL_PUSH_BODY_TOO_LARGE")
        if authorization is None or not authorization.startswith("Bearer "):
            raise GmailPushValidationError("GMAIL_PUSH_BEARER_REQUIRED")
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise GmailPushValidationError("GMAIL_PUSH_BEARER_REQUIRED")
        try:
            claims = await asyncio.to_thread(
                self._token_verifier, token, self._audience
            )
        except Exception as error:
            raise GmailPushValidationError("GMAIL_PUSH_TOKEN_INVALID") from error
        self._validate_claims(claims)
        return self._parse_envelope(body)

    def _validate_claims(self, claims: Mapping[str, Any]) -> None:
        if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
            raise GmailPushValidationError("GMAIL_PUSH_ISSUER_INVALID")
        if claims.get("aud") != self._audience:
            raise GmailPushValidationError("GMAIL_PUSH_AUDIENCE_INVALID")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise GmailPushValidationError("GMAIL_PUSH_SUBJECT_INVALID")
        email = claims.get("email")
        if not isinstance(email, str) or email.strip().lower() != self._service_account_email:
            raise GmailPushValidationError("GMAIL_PUSH_SERVICE_ACCOUNT_INVALID")
        if claims.get("email_verified") is not True:
            raise GmailPushValidationError("GMAIL_PUSH_EMAIL_UNVERIFIED")
        expiration = claims.get("exp")
        if not isinstance(expiration, (int, float)) or expiration <= datetime.now(UTC).timestamp():
            raise GmailPushValidationError("GMAIL_PUSH_TOKEN_EXPIRED")

    def _parse_envelope(self, body: bytes) -> VerifiedGmailNotification:
        try:
            envelope = json.loads(body)
            if not isinstance(envelope, dict):
                raise TypeError
            subscription = envelope["subscription"]
            message = envelope["message"]
            if not isinstance(subscription, str) or not isinstance(message, dict):
                raise TypeError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GmailPushValidationError("GMAIL_PUSH_ENVELOPE_INVALID") from error
        if subscription != self._subscription_name:
            raise GmailPushValidationError("GMAIL_PUSH_SUBSCRIPTION_INVALID")
        message_id = message.get("messageId")
        data = message.get("data")
        if not isinstance(message_id, str) or not message_id or not isinstance(data, str):
            raise GmailPushValidationError("GMAIL_PUSH_MESSAGE_INVALID")
        try:
            decoded = base64.b64decode(data, validate=True)
            notification = json.loads(decoded)
            mailbox = notification["emailAddress"]
            history_id = notification["historyId"]
            if not isinstance(mailbox, str) or not isinstance(history_id, str):
                raise TypeError
            int(history_id)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GmailPushValidationError("GMAIL_PUSH_DATA_INVALID") from error
        published_at = None
        raw_published_at = message.get("publishTime")
        if isinstance(raw_published_at, str):
            try:
                published_at = datetime.fromisoformat(raw_published_at.replace("Z", "+00:00"))
            except ValueError:
                raise GmailPushValidationError("GMAIL_PUSH_PUBLISH_TIME_INVALID") from None
        return VerifiedGmailNotification(
            subscription_name=subscription,
            pubsub_message_id=message_id,
            published_at=published_at,
            mailbox_address=mailbox.strip().lower(),
            history_id=history_id,
        )


class GmailPushService:
    def __init__(self, db_session: AsyncSession, *, receipt_retention_days: int) -> None:
        self._db_session = db_session
        self._receipt_retention_days = receipt_retention_days

    async def accept(self, notification: VerifiedGmailNotification) -> UUID | None:
        state = await self._db_session.scalar(
            select(EmailSyncState)
            .where(EmailSyncState.mailbox_address == notification.mailbox_address)
            .with_for_update()
        )
        if state is None:
            raise GmailPushMailboxNotFound
        receipt_id = await self._db_session.scalar(
            insert(GmailPushReceipt)
            .values(
                organization_id=state.organization_id,
                connector_id=state.connector_id,
                subscription_name=notification.subscription_name,
                pubsub_message_id=notification.pubsub_message_id,
                notification_history_id=notification.history_id,
                published_at=notification.published_at,
                expires_at=datetime.now(UTC)
                + timedelta(days=self._receipt_retention_days),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    GmailPushReceipt.subscription_name,
                    GmailPushReceipt.pubsub_message_id,
                ]
            )
            .returning(GmailPushReceipt.id)
        )
        if receipt_id is None:
            await self._db_session.commit()
            return None
        state.last_notification_at = datetime.now(UTC)
        notification_cursor = int(notification.history_id)
        if (
            state.processed_history_id is not None
            and notification_cursor <= int(state.processed_history_id)
        ):
            await self._db_session.commit()
            return None
        if not state.auto_sync_enabled:
            await self._db_session.commit()
            return None
        if state.pending_history_id is None or notification_cursor > int(
            state.pending_history_id
        ):
            state.pending_history_id = notification.history_id
        knowledge_base_id = await self._db_session.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.organization_id == state.organization_id
            )
        )
        if knowledge_base_id is None:
            raise RuntimeError("Gmail mailbox organization has no knowledge base")
        try:
            sync_request = await GmailSyncCoordinator(self._db_session).schedule_locked(
                state,
                knowledge_base_id=knowledge_base_id,
                trigger="PUSH",
            )
        except GmailSyncPaused:
            await self._db_session.commit()
            return None
        state.version += 1
        await self._db_session.commit()
        return sync_request.job.id
