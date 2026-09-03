from __future__ import annotations

import pytest

from app.modules.webhooks.delivery import retryable_for_attempt
from app.modules.webhooks.signing import WebhookSigner


@pytest.fixture
def signer() -> WebhookSigner:
    return WebhookSigner(b"s" * 32)


def test_signature_covers_timestamp_and_exact_body(signer: WebhookSigner) -> None:
    body = b'{"event_id":"00000000-0000-0000-0000-000000000001"}'

    signature = signer.sign(body=body, timestamp=1_800_000_000)

    assert signature.startswith("v1=")
    assert signer.verify(
        body=body,
        timestamp=1_800_000_000,
        signature=signature,
        now=1_800_000_100,
    )
    assert not signer.verify(
        body=body + b" ",
        timestamp=1_800_000_000,
        signature=signature,
        now=1_800_000_100,
    )


@pytest.mark.parametrize(
    ("timestamp", "now"),
    [
        (1_800_000_000, 1_800_000_301),
        (1_800_000_301, 1_800_000_000),
    ],
)
def test_verification_rejects_signatures_outside_five_minute_window(
    signer: WebhookSigner,
    timestamp: int,
    now: int,
) -> None:
    body = b"{}"

    assert not signer.verify(
        body=body,
        timestamp=timestamp,
        signature=signer.sign(body=body, timestamp=timestamp),
        now=now,
    )


@pytest.mark.parametrize(
    "signature",
    ["", "v2=deadbeef", "v1=not-hex", "deadbeef"],
)
def test_verification_rejects_malformed_or_unknown_signature_versions(
    signer: WebhookSigner,
    signature: str,
) -> None:
    assert not signer.verify(
        body=b"{}",
        timestamp=1_800_000_000,
        signature=signature,
        now=1_800_000_000,
    )


def test_signer_requires_a_strong_secret() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        WebhookSigner(b"short")


def test_delivery_retry_budget_is_bounded() -> None:
    assert retryable_for_attempt(requested=True, attempt=4)
    assert not retryable_for_attempt(requested=True, attempt=5)
    assert not retryable_for_attempt(requested=False, attempt=1)
