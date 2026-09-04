from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

# These codes can be persisted on JobIntent/DeliveryIntent or returned in the
# operational summary.  Operators need an actionable, non-secret response for
# each one before the platform is handed over.
OPERATOR_VISIBLE_ERROR_CODES = frozenset(
    {
        "CHAT_ANSWER_UNAVAILABLE",
        "CHAT_ANSWER_UNVALIDATED",
        "CHAT_JOB_INVALID",
        "CHAT_SAFETY_CLASSIFIER_UNAVAILABLE",
        "DELIVERY_RECONCILIATION_REQUIRED",
        "DOCUMENT_DOWNLOAD_FORBIDDEN",
        "DOCUMENT_FILE_MISMATCH",
        "DOCUMENT_PARSE_FAILED",
        "DOCUMENT_PARSE_JOB_UNAVAILABLE",
        "DOCUMENT_PARSE_TRANSIENT_FAILURE",
        "DOCUMENT_SOURCE_NOT_FOUND",
        "DRIVE_REAUTH_REQUIRED",
        "DRIVE_SOURCE_DISABLED",
        "DRIVE_SYNC_TRANSIENT_FAILURE",
        "EMAIL_APPROVAL_INVALID",
        "EMAIL_APPROVAL_INVALIDATED",
        "EMAIL_CLASSIFICATION_FAILED",
        "EMAIL_DRAFT_FAILED",
        "EMAIL_DRAFT_GROUNDED_ANSWER_UNAVAILABLE",
        "EMAIL_DRAFT_PRINCIPAL_SCOPE_MISMATCH",
        "EMAIL_DRAFT_UNAUTHORIZED_CITATION",
        "EMAIL_JOB_KIND_INVALID",
        "EMAIL_WORKER_TRANSIENT_FAILURE",
        "GMAIL_AUTHORIZATION_FAILED",
        "GMAIL_PREVIOUS_ATTEMPT_UNCERTAIN",
        "GMAIL_PRE_SEND_FAILURE",
        "GMAIL_REAUTH_REQUIRED",
        "GMAIL_RESPONSE_AMBIGUOUS",
        "GMAIL_RESPONSE_MISSING_MESSAGE_ID",
        "GMAIL_RESPONSE_MISSING_THREAD_ID",
        "HANDOFF_RESUME_STALE",
        "INVALID_DOCUMENT_PARSE_JOB",
        "JOB_INTENT_INVALID",
        "JOB_NOT_RETRYABLE",
        "JOB_VERSION_CONFLICT",
        "RETENTION_JOB_FAILED",
        "WEBHOOK_DELIVERY_ATTEMPTS_EXHAUSTED",
        "WEBHOOK_DELIVERY_REJECTED",
        "WEBHOOK_DELIVERY_RETRYABLE_RESPONSE",
        "WEBHOOK_DELIVERY_TRANSPORT_FAILED",
        "WEBHOOK_SUBSCRIPTION_DISABLED",
    }
)


def documented_error_codes(document: Path) -> set[str]:
    return set(re.findall(r"`([A-Z][A-Z0-9_]+)`", document.read_text(encoding="utf-8")))


def test_all_operator_visible_error_codes_are_documented() -> None:
    documented = documented_error_codes(ROOT / "docs/operations/incident-response.md")

    assert OPERATOR_VISIBLE_ERROR_CODES <= documented
