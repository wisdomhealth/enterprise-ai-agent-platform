import httpx
import openai
import pytest

from app.core.openai import OpenAIStructuredResponseError
from app.modules.email.classification import (
    EmailClassifierResponseError,
    classify_email_error,
)
from app.modules.jobs.models import ErrorClass


def _status_error(
    error_type: type[openai.APIStatusError],
    status_code: int,
    *,
    body: object,
    headers: dict[str, str] | None = None,
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")
    response = httpx.Response(status_code, request=request, headers=headers)
    return error_type("provider failure", response=response, body=body)


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (
            _status_error(
                openai.AuthenticationError,
                401,
                body={"error": {"code": "invalid_api_key"}},
            ),
            "EMAIL_CLASSIFICATION_AUTHENTICATION_FAILED",
        ),
        (
            _status_error(
                openai.PermissionDeniedError,
                403,
                body={"error": {"code": "model_not_found"}},
            ),
            "EMAIL_CLASSIFICATION_MODEL_ACCESS_DENIED",
        ),
        (
            _status_error(
                openai.NotFoundError,
                404,
                body={"error": {"code": "model_not_found"}},
            ),
            "EMAIL_CLASSIFICATION_MODEL_ACCESS_DENIED",
        ),
        (
            _status_error(
                openai.BadRequestError,
                400,
                body={"error": {"code": "invalid_request_error"}},
            ),
            "EMAIL_CLASSIFICATION_INVALID_REQUEST",
        ),
        (
            _status_error(
                openai.RateLimitError,
                429,
                body={"error": {"code": "insufficient_quota"}},
            ),
            "EMAIL_CLASSIFICATION_INSUFFICIENT_QUOTA",
        ),
    ],
)
def test_provider_configuration_and_quota_errors_are_terminal(
    error: Exception, error_code: str
) -> None:
    failure = classify_email_error(error)

    assert failure.error_code == error_code
    assert failure.error_class is ErrorClass.NON_RETRYABLE
    assert failure.retry_after_seconds is None
    assert failure.__cause__ is error


def test_temporary_rate_limit_is_retryable_and_honors_bounded_retry_after() -> None:
    error = _status_error(
        openai.RateLimitError,
        429,
        body={"error": {"code": "rate_limit_exceeded"}},
        headers={"retry-after": "900"},
    )

    failure = classify_email_error(error, retry_after_cap_seconds=300)

    assert failure.error_code == "EMAIL_CLASSIFICATION_RATE_LIMITED"
    assert failure.error_class is ErrorClass.RETRYABLE
    assert failure.retry_after_seconds == 300


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (
            openai.APITimeoutError(httpx.Request("POST", "https://api.openai.test")),
            "EMAIL_CLASSIFICATION_TIMEOUT",
        ),
        (
            openai.APIConnectionError(
                request=httpx.Request("POST", "https://api.openai.test")
            ),
            "EMAIL_CLASSIFICATION_CONNECTION_FAILED",
        ),
        (ConnectionError("socket reset"), "EMAIL_CLASSIFICATION_CONNECTION_FAILED"),
        (
            _status_error(
                openai.InternalServerError,
                503,
                body={"error": {"code": "server_error"}},
            ),
            "EMAIL_CLASSIFICATION_SERVER_ERROR",
        ),
    ],
)
def test_transient_provider_errors_are_retryable(error: Exception, error_code: str) -> None:
    failure = classify_email_error(error)

    assert failure.error_code == error_code
    assert failure.error_class is ErrorClass.RETRYABLE


def test_structured_output_failure_is_retryable_but_unclassified_failure_is_terminal() -> None:
    structured = EmailClassifierResponseError("invalid")
    structured.__cause__ = OpenAIStructuredResponseError()

    structured_failure = classify_email_error(structured)
    unknown_failure = classify_email_error(RuntimeError("unexpected"))

    assert structured_failure.error_code == "EMAIL_CLASSIFICATION_INVALID_RESPONSE"
    assert structured_failure.error_class is ErrorClass.RETRYABLE
    assert unknown_failure.error_code == "EMAIL_CLASSIFICATION_UNCLASSIFIED_FAILURE"
    assert unknown_failure.error_class is ErrorClass.NON_RETRYABLE
