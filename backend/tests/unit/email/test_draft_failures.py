import pytest

from app.modules.email.drafting import (
    classify_draft_exception,
    classify_grounded_refusal,
)
from app.modules.jobs.models import ErrorClass
from app.modules.rag.llm import ProviderTransientError


@pytest.mark.parametrize(
    ("outcome", "error_code", "error_class"),
    [
        ("no_evidence", "EMAIL_DRAFT_KNOWLEDGE_INSUFFICIENT", ErrorClass.NON_RETRYABLE),
        ("validation_refusal", "EMAIL_DRAFT_MODEL_SAFETY_REFUSAL", ErrorClass.NON_RETRYABLE),
        ("provider_transient_error", "EMAIL_DRAFT_MODEL_TEMPORARY_FAILURE", ErrorClass.RETRYABLE),
        ("provider_quota_error", "EMAIL_DRAFT_MODEL_QUOTA_EXHAUSTED", ErrorClass.NON_RETRYABLE),
        (
            "provider_configuration_error",
            "EMAIL_DRAFT_MODEL_CONFIGURATION_INVALID",
            ErrorClass.NON_RETRYABLE,
        ),
    ],
)
def test_grounded_refusal_outcomes_remain_distinct(
    outcome: str,
    error_code: str,
    error_class: ErrorClass,
) -> None:
    failure = classify_grounded_refusal(outcome)

    assert failure.error_code == error_code
    assert failure.error_class is error_class


@pytest.mark.parametrize(
    ("error", "error_code", "error_class"),
    [
        (TimeoutError(), "EMAIL_DRAFT_MODEL_TEMPORARY_FAILURE", ErrorClass.RETRYABLE),
        (ConnectionError(), "EMAIL_DRAFT_MODEL_TEMPORARY_FAILURE", ErrorClass.RETRYABLE),
        (
            ProviderTransientError(),
            "EMAIL_DRAFT_MODEL_TEMPORARY_FAILURE",
            ErrorClass.RETRYABLE,
        ),
        (ValueError(), "EMAIL_DRAFT_UNCLASSIFIED_FAILURE", ErrorClass.NON_RETRYABLE),
    ],
)
def test_only_known_temporary_draft_exceptions_are_retryable(
    error: Exception,
    error_code: str,
    error_class: ErrorClass,
) -> None:
    failure = classify_draft_exception(error)

    assert failure.error_code == error_code
    assert failure.error_class is error_class
