from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.email.models import (
    DeliveryAttempt,
    DeliveryIntent,
    EmailCategory,
    EmailPriority,
    EmailState,
)
from app.modules.email.review import EmailReviewService
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session
from app.modules.identity.models import StaffUser, UserRole, UserStatus


def _application(db_session: AsyncSession, principal: Principal):  # type: ignore[no-untyped-def]
    application = create_app(Settings.model_validate({"SESSION_SECRET": "task20-read-secret"}))

    async def override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def override_staff() -> Principal:
        return principal

    application.dependency_overrides[get_db_session] = override_db
    application.dependency_overrides[require_staff_session] = override_staff
    return application


@pytest.mark.asyncio
async def test_authorized_staff_reads_queue_and_safe_persisted_detail(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    principal = email_review_context["principal"]
    item.category = EmailCategory.ACTION_REQUIRED
    item.priority = EmailPriority.HIGH
    item.reply_required = True
    item.classification_provenance = {
        "model": "classifier-model",
        "prompt_version": "classifier-v1",
        "latency_ms": 12,
        "access_token": "must-not-leak",
    }
    draft.reviewer_instruction = "Keep it concise."
    draft.citations = [
        {
            "organization_id": str(item.organization_id),
            "knowledge_base_id": str(item.knowledge_base_id),
            "chunk_id": str(uuid4()),
            "document_version_id": str(uuid4()),
            "title": "Support policy",
            "section": "Response times",
            "page_number": 2,
            "internal_drive_link": "https://drive.google.com/open?id=safe-reference",
        },
        {
            "organization_id": str(item.organization_id),
            "knowledge_base_id": str(item.knowledge_base_id),
            "chunk_id": str(uuid4()),
            "document_version_id": str(uuid4()),
            "title": "Unsafe link is removed",
            "section": None,
            "page_number": None,
            "internal_drive_link": "https://attacker.example/internal",
        },
        {
            "organization_id": str(uuid4()),
            "knowledge_base_id": str(item.knowledge_base_id),
            "chunk_id": str(uuid4()),
            "document_version_id": str(uuid4()),
            "title": "Cross-tenant source",
            "section": None,
            "page_number": None,
            "internal_drive_link": "https://drive.google.com/open?id=cross-tenant",
        },
    ]
    await db_session.flush()

    application = _application(db_session, principal)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="https://testserver"
        ) as client:
            queue = await client.get("/api/v1/staff/email")
            detail = await client.get(f"/api/v1/staff/email/{item.id}")
    finally:
        application.dependency_overrides.clear()

    assert queue.status_code == 200
    assert queue.json() == [
        {
            "id": str(item.id),
            "state": "AWAITING_REVIEW",
            "version": item.version,
            "sender": item.sender,
            "subject": item.subject,
            "received_at": item.received_at.isoformat().replace("+00:00", "Z"),
            "category": "ACTION_REQUIRED",
            "priority": "HIGH",
        }
    ]
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["body"] == item.body
    assert payload["classification_rationale"] == (
        "Action Required · High priority · Reply required"
    )
    assert payload["current_draft_id"] == str(draft.id)
    assert payload["drafts"][0]["reviewer_instruction"] == "Keep it concise."
    assert payload["drafts"][0]["model"] == "claude-fixture"
    assert payload["drafts"][0]["prompt_version"] == "email-draft-v1"
    assert payload["drafts"][0]["citations"][0]["title"] == "Support policy"
    assert payload["drafts"][0]["citations"][1]["title"] == "Unsafe link is removed"
    assert payload["drafts"][0]["citations"][1]["internal_drive_link"] is None
    assert len(payload["drafts"][0]["citations"]) == 2
    assert payload["delivery"] is None
    serialized = str(payload)
    assert "raw_content_ref" not in serialized
    assert "must-not-leak" not in serialized
    assert "access_token" not in serialized
    assert str(item.organization_id) not in serialized
    assert str(item.knowledge_base_id) not in serialized
    assert "attacker.example" not in serialized
    assert "Cross-tenant source" not in serialized


@pytest.mark.asyncio
async def test_email_reads_enforce_reviewer_role_and_resource_grant(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    principal = email_review_context["principal"]
    grant = await db_session.scalar(
        select(ResourceGrant).where(ResourceGrant.subject_id == principal.subject_id)
    )
    assert grant is not None
    await db_session.delete(grant)
    await db_session.flush()

    application = _application(db_session, principal)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="https://testserver"
        ) as client:
            queue = await client.get("/api/v1/staff/email")
            detail = await client.get(f"/api/v1/staff/email/{item.id}")
    finally:
        application.dependency_overrides.clear()

    assert queue.status_code == 200
    assert queue.json() == []
    assert detail.status_code == 403


@pytest.mark.asyncio
async def test_email_reads_reject_member_even_with_resource_grant(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    organization = email_review_context["organization"]
    member = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"task20-member-{uuid4()}",
        email="task20-member@example.test",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(member)
    await db_session.flush()
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=member.id,
            resource_type="knowledge",
            resource_id=item.knowledge_base_id,
            actions=["knowledge.review"],
        )
    )
    await db_session.flush()
    principal = Principal(
        member.id,
        organization.id,
        member.email,
        member.role,
        uuid4(),
        "fixture-csrf",
    )

    application = _application(db_session, principal)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="https://testserver"
        ) as client:
            queue = await client.get("/api/v1/staff/email")
            detail = await client.get(f"/api/v1/staff/email/{item.id}")
    finally:
        application.dependency_overrides.clear()

    assert queue.status_code == 403
    assert detail.status_code == 403


@pytest.mark.asyncio
async def test_email_detail_projects_delivery_attempts_from_durable_records(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    principal = email_review_context["principal"]
    service = EmailReviewService(db_session, principal)
    approved = await service.approve(
        item.id,
        expected_version=item.version,
        current_draft_id=draft.id,
    )
    edited = await service.edit(
        item.id,
        expected_version=approved.version,
        current_draft_id=approved.current_draft_id,
        body="Current reviewed reply.",
    )
    reapproved = await service.approve(
        item.id,
        expected_version=edited.version,
        current_draft_id=edited.current_draft_id,
    )
    intent = await db_session.scalar(
        select(DeliveryIntent).where(
            DeliveryIntent.work_item_id == item.id,
            DeliveryIntent.approved_draft_version_id == reapproved.current_draft_id,
        )
    )
    assert intent is not None
    intent.state = EmailState.DELIVERY_UNKNOWN
    intent.last_error_code = "GMAIL_RESPONSE_TIMEOUT"
    intent.version += 1
    db_session.add(
        DeliveryAttempt(
            delivery_intent_id=intent.id,
            attempt_number=1,
            outcome="UNKNOWN",
            error_code="GMAIL_RESPONSE_TIMEOUT",
            started_at=datetime(2026, 9, 2, 8, 10, tzinfo=UTC),
            completed_at=datetime(2026, 9, 2, 8, 11, tzinfo=UTC),
        )
    )
    await db_session.flush()

    application = _application(db_session, principal)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="https://testserver"
        ) as client:
            detail = await client.get(f"/api/v1/staff/email/{item.id}")
    finally:
        application.dependency_overrides.clear()

    assert detail.status_code == 200
    delivery = detail.json()["delivery"]
    assert delivery == {
        "id": str(intent.id),
        "state": "DELIVERY_UNKNOWN",
        "version": intent.version,
        "deterministic_message_id": intent.deterministic_message_id,
        "last_error_code": "GMAIL_RESPONSE_TIMEOUT",
        "attempts": [
            {
                "id": delivery["attempts"][0]["id"],
                "attempt_number": 1,
                "outcome": "UNKNOWN",
                "error_code": "GMAIL_RESPONSE_TIMEOUT",
                "started_at": "2026-09-02T08:10:00Z",
                "completed_at": "2026-09-02T08:11:00Z",
            }
        ],
    }
