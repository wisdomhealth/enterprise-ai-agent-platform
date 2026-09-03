import pytest
from sqlalchemy import select, text

from app.modules.audit.models import AuditEvent
from app.modules.knowledge.models import DocumentChunk
from app.modules.retention.service import RetentionService


@pytest.mark.asyncio
async def test_due_retention_redacts_domain_content_and_not_knowledge(
    db_session, retention_context
) -> None:  # type: ignore[no-untyped-def]
    service = RetentionService(db_session)

    first = await service.apply_due(
        retention_context["organization"].id,
        now=retention_context["now"],
        batch_size=100,
    )
    second = await service.apply_due(
        retention_context["organization"].id,
        now=retention_context["now"],
        batch_size=100,
    )

    assert first.chat_messages == 1
    assert first.email_items == 1
    assert first.email_drafts == 1
    assert first.audit_events == 1
    assert second.total == 0
    assert retention_context["chat_message"].body == ""
    assert retention_context["email_item"].body == ""
    assert retention_context["draft"].body == ""
    assert await db_session.get(DocumentChunk, retention_context["chunk"].id) is not None
    assert await db_session.get(AuditEvent, retention_context["old_audit"].id) is None
    assert await db_session.get(AuditEvent, retention_context["recent_audit"].id) is not None


@pytest.mark.asyncio
async def test_admin_policy_is_resource_authorized_versioned_and_audited(
    db_session, retention_context
) -> None:  # type: ignore[no-untyped-def]
    admin = retention_context["admin"]
    policy = retention_context["policy"]
    policy_id = policy.id
    original_version = policy.version
    async with retention_context["client_for"](admin) as client:
        read = await client.get("/api/v1/admin/retention-policy")
        updated = await client.patch(
            "/api/v1/admin/retention-policy",
            headers={"Idempotency-Key": "retention-policy-update-1"},
            json={
                "expected_version": original_version,
                "chat_days": 45,
                "email_days": 60,
                "audit_days": 730,
            },
        )
        stale = await client.patch(
            "/api/v1/admin/retention-policy",
            headers={"Idempotency-Key": "retention-policy-update-stale"},
            json={
                "expected_version": original_version,
                "chat_days": 30,
                "email_days": 30,
                "audit_days": 365,
            },
        )

    assert read.status_code == 200
    assert read.json()["legal_compliance_guarantee"] is False
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert stale.status_code == 409
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "retention.policy.update", AuditEvent.object_id == policy_id
        )
    )
    assert audit is not None
    assert audit.details == {
        "previous": {"chat_days": 90, "email_days": 90, "audit_days": 365, "version": 1},
        "current": {"chat_days": 45, "email_days": 60, "audit_days": 730, "version": 2},
    }


@pytest.mark.asyncio
async def test_non_admin_cannot_discover_retention_policy(retention_context) -> None:  # type: ignore[no-untyped-def]
    async with retention_context["client_for"](retention_context["reviewer"]) as client:
        response = await client.get("/api/v1/admin/retention-policy")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_platform_application_role_can_execute_due_retention(
    db_session, retention_context
) -> None:  # type: ignore[no-untyped-def]
    await db_session.execute(text("SET LOCAL ROLE platform_app"))

    result = await RetentionService(db_session).apply_due(
        retention_context["organization"].id,
        now=retention_context["now"],
        batch_size=100,
    )

    assert result.chat_messages == 1
    assert result.email_items == 1
    assert result.email_drafts == 1
    assert result.audit_events == 1
