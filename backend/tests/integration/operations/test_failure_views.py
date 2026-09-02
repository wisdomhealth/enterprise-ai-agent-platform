from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.authorization.models import ResourceGrant
from app.modules.email.models import EmailEvaluationRun, EmailState, EmailWorkItem
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.operations.service import OperationsService  # noqa: F401


@pytest.mark.asyncio
async def test_failure_views_and_summary_expose_safe_status_not_bodies(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    organization = operations_context["organization"]
    source = operations_context["source"]
    connector = operations_context["connector"]
    job = JobIntent(
        kind="knowledge.drive_source.sync",
        idempotency_key=f"drive-failure-{uuid4()}",
        payload={"source_id": str(source.id), "secret": "must-not-leak"},
        state=JobState.FAILED,
        last_error_code="provider said private-error-detail",
        error_class=ErrorClass.RETRYABLE,
        attempts=2,
    )
    email = EmailWorkItem(
        organization_id=organization.id,
        connector_id=connector.id,
        knowledge_base_id=source.knowledge_base_id,
        gmail_message_id=f"safe-view-{uuid4()}",
        gmail_thread_id="thread-safe-view",
        sender="customer@example.test",
        recipients=["support@example.test"],
        subject="private subject must not appear",
        body="private body must not appear",
        received_at=datetime.now(UTC),
        raw_content_ref="gmail://secret-ref",
        state=EmailState.DRAFT_RETRY_WAIT,
        last_error_code="EMAIL_MODEL_RATE_LIMITED",
    )
    review_grant = ResourceGrant(
        organization_id=organization.id,
        subject_id=operations_context["admin"].id,
        resource_type="knowledge",
        resource_id=source.knowledge_base_id,
        actions=["knowledge.review"],
    )
    email_quality = EmailEvaluationRun(
        dataset_version="task21-visible-quality",
        dataset_kind="regression",
        dataset_digest="2" * 64,
        model="safe-model-id",
        prompt_version="safe-prompt-id",
        macro_f1=0.91,
        structured_output_success=1.0,
        latency_ms=12,
        input_tokens=1,
        output_tokens=1,
        estimated_cost=0.02,
    )
    db_session.add_all((job, email, review_grant, email_quality))
    await db_session.commit()

    async with operations_context["client_for"](operations_context["admin"]) as client:
        failures = await client.get("/api/v1/admin/jobs/failed")
        summary = await client.get("/api/v1/admin/operations/summary")

    assert failures.status_code == 200
    assert failures.json()[0]["job_id"] == str(job.id)
    assert failures.json()[0]["action"] == "RETRY_DRIVE_SYNC"
    assert failures.json()[0]["error_code"] == "UNSAFE_ERROR_REDACTED"
    serialized = f"{failures.text} {summary.text}"
    assert "must-not-leak" not in serialized
    assert "private subject" not in serialized
    assert "private body" not in serialized
    assert "gmail://" not in serialized
    assert "private-error-detail" not in serialized
    assert summary.json()["knowledge_sources"][0]["cursor"] == "drive-cursor-9"
    assert summary.json()["email"]["retry_wait"] == 1
    assert summary.json()["email_quality"]["quality_score"] == 0.91
