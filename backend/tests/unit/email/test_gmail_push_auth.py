import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.email.push import GmailPushAuthenticator, GmailPushValidationError


def _body(*, subscription: str = "projects/p/subscriptions/gmail") -> bytes:
    data = base64.b64encode(
        json.dumps({"emailAddress": "Staff@Example.test ", "historyId": "901"}).encode()
    ).decode()
    return json.dumps(
        {
            "subscription": subscription,
            "message": {
                "messageId": "pubsub-1",
                "publishTime": "2026-10-19T00:00:00Z",
                "data": data,
            },
        }
    ).encode()


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": "https://platform.example/api/v1/webhooks/gmail/pubsub",
        "sub": "stable-service-account-subject",
        "email": "gmail-push@example.iam.gserviceaccount.com",
        "email_verified": True,
        "exp": (datetime.now(UTC) + timedelta(minutes=5)).timestamp(),
    }
    claims.update(overrides)
    return claims


def _authenticator(claims: dict[str, object]) -> GmailPushAuthenticator:
    return GmailPushAuthenticator(
        audience="https://platform.example/api/v1/webhooks/gmail/pubsub",
        service_account_email="gmail-push@example.iam.gserviceaccount.com",
        subscription_name="projects/p/subscriptions/gmail",
        max_body_bytes=65_536,
        token_verifier=lambda _token, _audience: claims,
    )


@pytest.mark.asyncio
async def test_valid_push_identity_and_envelope_are_normalized() -> None:
    notification = await _authenticator(_claims()).verify("Bearer token", _body())

    assert notification.mailbox_address == "staff@example.test"
    assert notification.history_id == "901"
    assert notification.pubsub_message_id == "pubsub-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "code"),
    [
        (_claims(iss="attacker"), "GMAIL_PUSH_ISSUER_INVALID"),
        (_claims(aud="wrong"), "GMAIL_PUSH_AUDIENCE_INVALID"),
        (_claims(email_verified=False), "GMAIL_PUSH_EMAIL_UNVERIFIED"),
        (_claims(email="wrong@example.test"), "GMAIL_PUSH_SERVICE_ACCOUNT_INVALID"),
        (_claims(exp=0), "GMAIL_PUSH_TOKEN_EXPIRED"),
    ],
)
async def test_invalid_push_claims_are_rejected(
    claims: dict[str, object], code: str
) -> None:
    with pytest.raises(GmailPushValidationError, match=code):
        await _authenticator(claims).verify("Bearer token", _body())


@pytest.mark.asyncio
async def test_subscription_is_checked_after_identity() -> None:
    with pytest.raises(GmailPushValidationError, match="GMAIL_PUSH_SUBSCRIPTION_INVALID"):
        await _authenticator(_claims()).verify(
            "Bearer token", _body(subscription="projects/p/subscriptions/other")
        )
