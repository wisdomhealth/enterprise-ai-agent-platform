from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
from secrets import token_urlsafe
from uuid import UUID


class InvalidChatToken(ValueError):
    """Raised only by the in-process token test helper."""


@dataclass(frozen=True, slots=True)
class ChatTokenClaims:
    session_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedChatToken:
    session_id: UUID
    value: str
    token_hash: str
    expires_at: datetime


def hash_chat_token(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def derive_idempotent_chat_token(
    *, session_secret: str, idempotency_record_id: UUID, operation: str, session_id: UUID
) -> str:
    """Derive a replayable 256-bit credential without storing a bearer value."""

    if not session_secret:
        raise ValueError("session secret is required for idempotent chat credentials")
    message = b"enterprise-ai-agent-platform/public-chat-credential/v1\0" + b"\0".join(
        (operation.encode("utf-8"), idempotency_record_id.bytes, session_id.bytes)
    )
    digest = hmac_new(session_secret.encode("utf-8"), message, sha256).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class ChatTokenService:
    """Issues random credentials; the credential itself contains no claims.

    Production request authorization deliberately uses only ``hash_chat_token``
    against the durable credential row.  The small in-process registry supports
    pure token contract tests without changing that authorization boundary.
    """

    def __init__(self) -> None:
        self._issued: dict[str, ChatTokenClaims] = {}

    def issue(
        self,
        *,
        session_id: UUID,
        lifetime_seconds: int = 3_600,
        value: str | None = None,
    ) -> IssuedChatToken:
        expires_at = datetime.now(UTC) + timedelta(seconds=lifetime_seconds)
        credential_value = value or token_urlsafe(32)
        self._issued[credential_value] = ChatTokenClaims(
            session_id=session_id, expires_at=expires_at
        )
        return IssuedChatToken(
            session_id=session_id,
            value=credential_value,
            token_hash=hash_chat_token(credential_value),
            expires_at=expires_at,
        )

    def verify(self, value: str) -> ChatTokenClaims:
        claims = self._issued.get(value)
        if claims is None or claims.expires_at <= datetime.now(UTC):
            raise InvalidChatToken("invalid or expired chat session credential")
        return claims
