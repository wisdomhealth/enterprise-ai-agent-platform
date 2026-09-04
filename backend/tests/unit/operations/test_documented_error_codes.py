from __future__ import annotations

import json
import re
import runpy
from pathlib import Path
from typing import Any

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


CHECK_DOCUMENTATION: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/check-documentation"))


def test_documentation_check_rejects_missing_current_openapi_route() -> None:
    """The checked-in artifact cannot silently omit a route from create_app()."""
    current_schema = json.loads((ROOT / "docs/api/openapi.json").read_text(encoding="utf-8"))
    checked_schema = json.loads(json.dumps(current_schema))
    del checked_schema["paths"]["/api/v1/admin/users/{user_id}"]
    failures: list[str] = []

    CHECK_DOCUMENTATION["_check_openapi_artifact"](checked_schema, current_schema, failures)

    assert "OpenAPI artifact drift: run scripts/export-openapi" in failures


def test_documentation_check_requires_settings_alias_ownership() -> None:
    """Ownership coverage follows Settings aliases, including non-example values."""
    path = ROOT / "docs/deployment/credential-ownership.md"
    rows = CHECK_DOCUMENTATION["_environment_rows"](path.read_text(encoding="utf-8"))
    del rows["ANTHROPIC_BASE_URL"]
    failures: list[str] = []

    CHECK_DOCUMENTATION["_check_environment_rows"](rows, failures)

    assert any("ANTHROPIC_BASE_URL" in failure for failure in failures)
