from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest, new


class WebhookSigner:
    VERSION = "v1"
    DEFAULT_WINDOW_SECONDS = 300

    def __init__(self, secret: bytes, *, window_seconds: int = DEFAULT_WINDOW_SECONDS) -> None:
        if len(secret) < 32:
            raise ValueError("webhook signing secret must be at least 32 bytes")
        if window_seconds <= 0:
            raise ValueError("verification window must be positive")
        self._secret = secret
        self._window_seconds = window_seconds

    def sign(self, *, body: bytes, timestamp: int) -> str:
        digest = new(
            self._secret,
            str(timestamp).encode("ascii") + b"." + body,
            sha256,
        ).hexdigest()
        return f"{self.VERSION}={digest}"

    def verify(
        self,
        *,
        body: bytes,
        timestamp: int,
        signature: str,
        now: int,
    ) -> bool:
        if abs(now - timestamp) > self._window_seconds:
            return False
        prefix = f"{self.VERSION}="
        if not signature.startswith(prefix):
            return False
        supplied = signature.removeprefix(prefix)
        if len(supplied) != 64:
            return False
        try:
            bytes.fromhex(supplied)
        except ValueError:
            return False
        return compare_digest(
            signature,
            self.sign(body=body, timestamp=timestamp),
        )
