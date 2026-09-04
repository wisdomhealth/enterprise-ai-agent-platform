from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditEvent
from app.modules.identity.models import StaffSession, StaffUser, UserRole, UserStatus
from app.modules.outbox.models import OutboxEvent


@pytest.mark.asyncio
async def test_only_admin_can_invite_and_change_staff_roles(operations_context) -> None:  # type: ignore[no-untyped-def]
    async with operations_context["client_for"](operations_context["reviewer"]) as client:
        denied = await client.post(
            "/api/v1/admin/users/invitations",
            headers={"Idempotency-Key": "denied-invite"},
            json={"email": "new@example.com", "role": "MEMBER"},
        )
    async with operations_context["client_for"](operations_context["admin"]) as client:
        created = await client.post(
            "/api/v1/admin/users/invitations",
            headers={"Idempotency-Key": "accepted-invite"},
            json={"email": "new@example.com", "role": "MEMBER"},
        )
        replayed = await client.post(
            "/api/v1/admin/users/invitations",
            headers={"Idempotency-Key": "accepted-invite"},
            json={"email": "new@example.com", "role": "MEMBER"},
        )
        changed = await client.patch(
            f"/api/v1/admin/users/{created.json()['id']}",
            headers={"Idempotency-Key": "promote-invite"},
            json={"expected_version": created.json()["version"], "role": "REVIEWER"},
        )

    assert denied.status_code == 404
    assert created.status_code == 201
    assert replayed.status_code == 201
    assert replayed.json() == created.json()
    assert created.json()["status"] == "INVITED"
    assert changed.status_code == 200
    assert changed.json()["role"] == "REVIEWER"
    assert changed.json()["version"] == created.json()["version"] + 1


@pytest.mark.asyncio
async def test_disable_is_versioned_revokes_sessions_and_records_safe_evidence(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    organization = operations_context["organization"]
    target = StaffUser(
        organization_id=organization.id,
        oidc_subject="disable-target",
        email="disable@example.test",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(target)
    await db_session.flush()
    target_session = StaffSession(
        user_id=target.id,
        csrf_hash="target-csrf",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(target_session)
    await db_session.commit()
    target_id = target.id
    target_version = target.version
    target_session_id = target_session.id

    async with operations_context["client_for"](operations_context["admin"]) as client:
        response = await client.patch(
            f"/api/v1/admin/users/{target_id}",
            headers={"Idempotency-Key": "disable-target-user"},
            json={"expected_version": target_version, "status": "DISABLED"},
        )

    assert response.status_code == 200
    db_session.expire_all()
    disabled = await db_session.get(StaffUser, target_id)
    revoked_session = await db_session.get(StaffSession, target_session_id)
    assert disabled is not None and disabled.status is UserStatus.DISABLED
    assert revoked_session is not None and revoked_session.revoked_at is not None
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "identity.user.update", AuditEvent.object_id == target_id
        )
    )
    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "identity.user.updated",
            OutboxEvent.aggregate_id == target_id,
        )
    )
    assert audit is not None and "csrf" not in str(audit.details).lower()
    assert event is not None and "csrf" not in str(event.payload).lower()


@pytest.mark.asyncio
async def test_stale_or_cross_organization_user_update_fails_without_disclosure(
    operations_context,
) -> None:  # type: ignore[no-untyped-def]
    reviewer = operations_context["reviewer"]
    foreign = operations_context["foreign_admin"]
    reviewer_id = reviewer.id
    reviewer_version = reviewer.version
    foreign_id = foreign.id
    foreign_version = foreign.version
    async with operations_context["client_for"](operations_context["admin"]) as client:
        stale = await client.patch(
            f"/api/v1/admin/users/{reviewer_id}",
            headers={"Idempotency-Key": "stale-user-update"},
            json={"expected_version": reviewer_version + 10, "role": "MEMBER"},
        )
        hidden = await client.patch(
            f"/api/v1/admin/users/{foreign_id}",
            headers={"Idempotency-Key": "foreign-user-update"},
            json={"expected_version": foreign_version, "role": "MEMBER"},
        )

    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "RESOURCE_VERSION_CONFLICT",
        "state": "ACTIVE",
        "version": reviewer_version,
    }
    assert hidden.status_code == 404
