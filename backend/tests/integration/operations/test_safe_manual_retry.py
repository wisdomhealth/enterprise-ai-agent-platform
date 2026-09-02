from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import ConnectorStatus
from app.modules.email.models import (
    DeliveryIntent,
    EmailApproval,
    EmailDraftVersion,
    EmailState,
    EmailWorkItem,
)
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.outbox.models import OutboxEvent


@pytest.mark.asyncio
async def test_manual_sync_records_audit_and_durable_dispatch_intent(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    source = operations_context["source"]
    admin = operations_context["admin"]
    async with operations_context["client_for"](admin) as client:
        response = await client.post(f"/api/v1/admin/knowledge-sources/{source.id}/sync")

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert await db_session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.action == "knowledge.drive_source.sync.request",
            AuditEvent.object_id == source.id,
        )
    )
    assert await db_session.scalar(
        select(OutboxEvent.event_id).where(
            OutboxEvent.event_type == "knowledge.drive_source.sync.requested",
            OutboxEvent.aggregate_id == UUID(job_id),
        )
    )


@pytest.mark.asyncio
async def test_drive_scope_configuration_uses_existing_connector_and_emits_outbox(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    connector = operations_context["connector"]
    connector.status = ConnectorStatus.ACTIVE
    await db_session.commit()

    async with operations_context["client_for"](operations_context["admin"]) as client:
        response = await client.put(
            "/api/v1/admin/knowledge-sources/drive",
            json={"root_folder_id": "new-approved-root", "include_descendants": True},
        )

    assert response.status_code == 200
    assert response.json()["root_folder_id"] == "new-approved-root"
    assert response.json()["include_descendants"] is True
    assert await db_session.scalar(
        select(OutboxEvent.event_id).where(
            OutboxEvent.event_type == "knowledge.drive_source.configured",
            OutboxEvent.aggregate_id == UUID(response.json()["id"]),
        )
    )


@pytest.mark.asyncio
async def test_manual_retry_uses_original_job_intent(db_session, operations_context) -> None:  # type: ignore[no-untyped-def]
    source = operations_context["source"]
    job = JobIntent(
        kind="knowledge.drive_source.sync",
        idempotency_key=f"drive-retry-{uuid4()}",
        payload={"source_id": str(source.id), "page_token": "cursor-old"},
        state=JobState.FAILED,
        attempts=3,
        last_error_code="DRIVE_RATE_LIMITED",
        error_class=ErrorClass.RETRYABLE,
    )
    db_session.add(job)
    await db_session.commit()
    original_payload = dict(job.payload)
    original_key = job.idempotency_key
    job_id = job.id

    async with operations_context["client_for"](operations_context["admin"]) as client:
        response = await client.post(
            f"/api/v1/admin/jobs/{job_id}/retry",
            headers={"Idempotency-Key": "retry-original-drive-job"},
        )

    assert response.status_code == 202
    db_session.expire_all()
    retried = await db_session.get(JobIntent, job_id)
    assert retried is not None
    assert retried.state is JobState.PENDING
    assert retried.payload == original_payload
    assert retried.idempotency_key == original_key
    assert await db_session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.action == "knowledge.drive_source.sync.retry",
            AuditEvent.object_id == job_id,
        )
    )
    assert await db_session.scalar(
        select(OutboxEvent.event_id).where(
            OutboxEvent.event_type == "knowledge.drive_source.sync.requested",
            OutboxEvent.aggregate_id == job_id,
        )
    )


@pytest.mark.asyncio
async def test_safe_email_retry_uses_delivery_owner_service(db_session, operations_context) -> None:  # type: ignore[no-untyped-def]
    organization = operations_context["organization"]
    source = operations_context["source"]
    connector = operations_context["connector"]
    admin = operations_context["admin"]
    item = EmailWorkItem(
        organization_id=organization.id,
        connector_id=connector.id,
        knowledge_base_id=source.knowledge_base_id,
        gmail_message_id=f"safe-retry-{uuid4()}",
        gmail_thread_id="thread-safe-retry",
        sender="customer@example.test",
        recipients=["support@example.test"],
        subject="Safe retry",
        body="Safe fixture body",
        received_at=datetime.now(UTC),
        raw_content_ref="gmail://fixture/safe-retry",
        state=EmailState.SEND_RETRY_WAIT,
    )
    db_session.add(item)
    await db_session.flush()
    draft = EmailDraftVersion(
        work_item_id=item.id,
        organization_id=organization.id,
        version=1,
        body="Draft",
        to=["customer@example.test"],
        cc=[],
        subject="Re: Safe retry",
        thread_id=item.gmail_thread_id,
        model="fixture",
        prompt_version="fixture-v1",
        created_by_id=admin.id,
        creator_type="STAFF",
    )
    db_session.add(draft)
    await db_session.flush()
    item.current_draft_id = draft.id
    approval = EmailApproval(
        work_item_id=item.id,
        organization_id=organization.id,
        draft_version_id=draft.id,
        reviewer_id=admin.id,
    )
    db_session.add(approval)
    await db_session.flush()
    job = JobIntent(
        kind="email.delivery",
        idempotency_key=f"safe-delivery-{uuid4()}",
        payload={},
        state=JobState.PENDING,
        last_error_code="GMAIL_RATE_LIMITED",
        error_class=ErrorClass.RETRYABLE,
    )
    db_session.add(job)
    await db_session.flush()
    intent = DeliveryIntent(
        organization_id=organization.id,
        work_item_id=item.id,
        approved_draft_version_id=draft.id,
        approval_id=approval.id,
        job_id=job.id,
        deterministic_message_id=f"<delivery-{uuid4()}@mail.invalid>",
        state=EmailState.SEND_RETRY_WAIT,
        last_error_code="GMAIL_RATE_LIMITED",
    )
    job.payload = {
        "delivery_intent_id": str(intent.id),
        "organization_id": str(organization.id),
    }
    db_session.add(intent)
    await db_session.commit()
    job_id = job.id
    intent_id = intent.id
    organization_id = organization.id
    admin_id = admin.id
    knowledge_base_id = source.knowledge_base_id

    async with operations_context["client_for"](admin) as client:
        unauthorized = await client.post(
            f"/api/v1/admin/jobs/{job_id}/retry",
            headers={"Idempotency-Key": "email-retry-without-resource-grant"},
        )

    assert unauthorized.status_code == 404
    db_session.add(
        ResourceGrant(
            organization_id=organization_id,
            subject_id=admin_id,
            resource_type="knowledge",
            resource_id=knowledge_base_id,
            actions=["knowledge.review"],
        )
    )
    await db_session.commit()
    await db_session.refresh(admin)
    await db_session.refresh(operations_context["session"])

    async with operations_context["client_for"](admin) as client:
        listed = await client.get("/api/v1/admin/jobs/failed")
        retried = await client.post(
            f"/api/v1/admin/jobs/{job_id}/retry",
            headers={"Idempotency-Key": "safe-email-retry"},
        )

    visible = next(entry for entry in listed.json() if entry["job_id"] == str(job_id))
    assert visible["action"] == "RETRY_EMAIL_DELIVERY"
    assert retried.status_code == 202
    db_session.expire_all()
    updated = await db_session.get(DeliveryIntent, intent_id)
    assert updated is not None and updated.state is EmailState.SEND_PENDING


@pytest.mark.asyncio
async def test_delivery_unknown_requires_reconciliation_and_never_retries_send(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    organization = operations_context["organization"]
    source = operations_context["source"]
    connector = operations_context["connector"]
    admin = operations_context["admin"]
    item = EmailWorkItem(
        organization_id=organization.id,
        connector_id=connector.id,
        knowledge_base_id=source.knowledge_base_id,
        gmail_message_id=f"delivery-unknown-{uuid4()}",
        gmail_thread_id="thread-unknown",
        sender="customer@example.test",
        recipients=["support@example.test"],
        subject="Unknown delivery",
        body="Safe fixture body",
        received_at=datetime.now(UTC),
        raw_content_ref="gmail://fixture/unknown",
        state=EmailState.DELIVERY_UNKNOWN,
    )
    db_session.add(item)
    await db_session.flush()
    draft = EmailDraftVersion(
        work_item_id=item.id,
        organization_id=organization.id,
        version=1,
        body="Draft",
        to=["customer@example.test"],
        cc=[],
        subject="Re: Unknown delivery",
        thread_id=item.gmail_thread_id,
        model="fixture",
        prompt_version="fixture-v1",
        created_by_id=admin.id,
        creator_type="STAFF",
    )
    db_session.add(draft)
    await db_session.flush()
    item.current_draft_id = draft.id
    approval = EmailApproval(
        work_item_id=item.id,
        organization_id=organization.id,
        draft_version_id=draft.id,
        reviewer_id=admin.id,
    )
    db_session.add(approval)
    await db_session.flush()
    job = JobIntent(
        kind="email.delivery",
        idempotency_key=f"unknown-delivery-{uuid4()}",
        payload={},
        state=JobState.RECONCILIATION,
        last_error_code="GMAIL_RESPONSE_TIMEOUT",
        error_class=ErrorClass.AMBIGUOUS,
    )
    db_session.add(job)
    await db_session.flush()
    intent = DeliveryIntent(
        organization_id=item.organization_id,
        work_item_id=item.id,
        approved_draft_version_id=draft.id,
        approval_id=approval.id,
        job_id=job.id,
        deterministic_message_id=f"<delivery-{uuid4()}@mail.invalid>",
        state=EmailState.DELIVERY_UNKNOWN,
    )
    job.payload = {
        "delivery_intent_id": str(intent.id),
        "organization_id": str(item.organization_id),
    }
    db_session.add(intent)
    await db_session.commit()
    job_id = job.id
    intent_id = intent.id

    async with operations_context["client_for"](operations_context["admin"]) as client:
        response = await client.post(
            f"/api/v1/admin/jobs/{job_id}/retry",
            headers={"Idempotency-Key": "must-reconcile-not-send"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "DELIVERY_RECONCILIATION_REQUIRED",
        "delivery_intent_id": str(intent_id),
    }
    db_session.expire_all()
    unchanged = await db_session.get(DeliveryIntent, intent_id)
    assert unchanged is not None
    assert unchanged.state is EmailState.DELIVERY_UNKNOWN
    assert unchanged.attempts == 0
