from __future__ import annotations

import logging
from io import StringIO

from app.core.logging import configure_logging, log_event


def test_structured_log_redacts_content_and_credentials() -> None:
    stream = StringIO()
    configure_logging(stream)

    log_event(
        "provider.failed",
        refresh_token="secret-token",
        email_body="private email",
        chat_body="private chat",
        nested={"authorization": "Bearer private", "document_content": "private document"},
        error_code="provider_timeout",
    )

    rendered = stream.getvalue()
    for sensitive in (
        "secret-token",
        "private email",
        "private chat",
        "Bearer private",
        "private document",
    ):
        assert sensitive not in rendered
    assert "provider_timeout" in rendered
    assert "provider.failed" in rendered


def test_redaction_does_not_destroy_safe_identifiers_or_error_codes() -> None:
    stream = StringIO()
    configure_logging(stream)

    log_event(
        "job.failed",
        event_id="00000000-0000-0000-0000-000000000001",
        message_id="00000000-0000-0000-0000-000000000002",
        error_code="PROVIDER_TIMEOUT",
        prompt="do not retain this prompt",
    )

    rendered = stream.getvalue()
    assert "00000000-0000-0000-0000-000000000001" in rendered
    assert "00000000-0000-0000-0000-000000000002" in rendered
    assert "PROVIDER_TIMEOUT" in rendered
    assert "do not retain this prompt" not in rendered


def test_access_log_strips_query_secrets_but_keeps_safe_request_metadata() -> None:
    stream = StringIO()
    configure_logging(stream)

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:1234",
        "GET",
        "/auth/callback?code=private-oauth-code",
        "1.1",
        200,
    )

    rendered = stream.getvalue()
    assert "private-oauth-code" not in rendered
    assert "/auth/callback" in rendered
    assert "GET" in rendered
