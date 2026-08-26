from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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


class ChatTokenService:
    """Issues random credentials; the credential itself contains no claims.

    Production request authorization deliberately uses only ``hash_chat_token``
    against the durable credential row.  The small in-process registry supports
    pure token contract tests without changing that authorization boundary.
    """

    def __init__(self) -> None:
        self._issued: dict[str, ChatTokenClaims] = {}

    def issue(self, *, session_id: UUID, lifetime_seconds: int = 3_600) -> IssuedChatToken:
        expires_at = datetime.now(UTC) + timedelta(seconds=lifetime_seconds)
        value = token_urlsafe(32)
        self._issued[value] = ChatTokenClaims(session_id=session_id, expires_at=expires_at)
        return IssuedChatToken(
            session_id=session_id,
            value=value,
            token_hash=hash_chat_token(value),
            expires_at=expires_at,
        )

    def verify(self, value: str) -> ChatTokenClaims:
        claims = self._issued.get(value)
        if claims is None or claims.expires_at <= datetime.now(UTC):
            raise InvalidChatToken("invalid or expired chat session credential")
        return claims
