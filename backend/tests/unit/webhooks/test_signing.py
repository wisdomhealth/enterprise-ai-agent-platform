from __future__ import annotations

import pytest

from app.modules.webhooks import delivery as webhook_delivery
from app.modules.webhooks.delivery import (
    HttpxWebhookTransport,
    _validated_endpoint_url,
    retryable_for_attempt,
)
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


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://127.0.0.1/webhook",
        "https://10.0.0.1/webhook",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/webhook",
        "https://[fe80::1]/webhook",
        "https://0.0.0.0/webhook",
        "https://224.0.0.1/webhook",
    ],
)
def test_endpoint_validation_rejects_non_global_ip_addresses(endpoint_url: str) -> None:
    with pytest.raises(ValueError, match="public"):
        _validated_endpoint_url(endpoint_url)


@pytest.mark.asyncio
async def test_request_time_resolution_blocks_hostname_rebinding_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def private_resolution(*_args: object) -> tuple[str, ...]:
        return ("10.0.0.1",)

    class FakeResponse:
        status_code = 204
        content = b"accepted"
        headers: dict[str, str] = {}

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            nonlocal called
            called = True
            return FakeResponse()

    monkeypatch.setattr(
        webhook_delivery,
        "_resolve_endpoint_addresses",
        private_resolution,
        raising=False,
    )
    monkeypatch.setattr(webhook_delivery.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    with pytest.raises(ValueError, match="public"):
        await HttpxWebhookTransport().post(
            url="https://delivery.example.test/webhook",
            body=b"{}",
            headers={},
        )
    assert not called
