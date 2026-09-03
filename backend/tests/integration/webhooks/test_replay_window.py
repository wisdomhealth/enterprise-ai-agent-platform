from __future__ import annotations

from app.modules.webhooks.signing import WebhookSigner


def test_redelivery_gets_a_fresh_signature_but_replay_window_remains_bounded() -> None:
    signer = WebhookSigner(b"r" * 32)
    body = b'{"event_id":"00000000-0000-0000-0000-000000000001"}'
    old_signature = signer.sign(body=body, timestamp=1_800_000_000)
    redelivery_signature = signer.sign(body=body, timestamp=1_800_000_400)

    assert not signer.verify(
        body=body,
        timestamp=1_800_000_000,
        signature=old_signature,
        now=1_800_000_400,
    )
    assert signer.verify(
        body=body,
        timestamp=1_800_000_400,
        signature=redelivery_signature,
        now=1_800_000_400,
    )
    assert old_signature != redelivery_signature
