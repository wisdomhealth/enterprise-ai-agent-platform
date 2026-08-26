from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.chat.tokens import ChatTokenService, InvalidChatToken


@pytest.fixture
def token_service() -> ChatTokenService:
    return ChatTokenService()


def test_chat_token_is_opaque_scoped_and_expires(token_service: ChatTokenService) -> None:
    issued = token_service.issue(session_id=uuid4(), lifetime_seconds=3_600)

    claims = token_service.verify(issued.value)

    assert claims.session_id == issued.session_id
    assert claims.expires_at > datetime.now(UTC)
    assert str(issued.session_id) not in issued.value
    assert len(issued.value) >= 43


def test_chat_token_rejects_tampering_and_expiry(token_service: ChatTokenService) -> None:
    issued = token_service.issue(session_id=uuid4(), lifetime_seconds=0)

    with pytest.raises(InvalidChatToken):
        token_service.verify(issued.value)
    with pytest.raises(InvalidChatToken):
        token_service.verify("not-a-chat-session-token")
