from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.email.models import EmailApproval, EmailState
from app.modules.email.review import EmailReviewAuthorizationError, EmailReviewService
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_csrf
from app.modules.identity.models import StaffUser, UserRole, UserStatus


@pytest.mark.asyncio
async def test_approval_requires_reviewer_and_resource_grant(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    organization = email_review_context["organization"]
    member = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"member-{uuid4()}",
        email="member@example.test",
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
    member_principal = Principal(
        member.id, organization.id, member.email, member.role, uuid4(), "csrf"
    )

    with pytest.raises(EmailReviewAuthorizationError):
        await EmailReviewService(db_session, member_principal).approve(
            item.id,
            expected_version=item.version,
            current_draft_id=draft.id,
        )
    assert item.state is EmailState.AWAITING_REVIEW
    assert await db_session.scalar(select(EmailApproval.id)) is None


@pytest.mark.asyncio
async def test_reviewer_without_resource_grant_is_denied(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    principal = email_review_context["principal"]
    await db_session.execute(
        ResourceGrant.__table__.delete().where(ResourceGrant.subject_id == principal.subject_id)
    )

    with pytest.raises(EmailReviewAuthorizationError):
        await EmailReviewService(db_session, principal).approve(
            item.id,
            expected_version=item.version,
            current_draft_id=draft.id,
        )


@pytest.mark.asyncio
async def test_review_tables_have_minimum_platform_app_privileges(
    db_session: AsyncSession,
) -> None:
    assert await db_session.scalar(
        select(text("has_table_privilege('platform_app', 'email_draft_versions', 'SELECT')"))
    )
    assert await db_session.scalar(
        select(text("has_table_privilege('platform_app', 'email_draft_versions', 'INSERT')"))
    )
    assert not await db_session.scalar(
        select(text("has_table_privilege('platform_app', 'email_draft_versions', 'UPDATE')"))
    )
    assert await db_session.scalar(
        select(text("has_table_privilege('platform_app', 'email_approvals', 'UPDATE')"))
    )


@pytest.mark.asyncio
async def test_approval_api_requires_idempotency_and_replays_safe_metadata(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    principal = email_review_context["principal"]
    application = create_app(Settings.model_validate({"SESSION_SECRET": "task18-secret"}))

    async def override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def override_staff() -> Principal:
        return principal

    application.dependency_overrides[get_db_session] = override_db
    application.dependency_overrides[require_staff_csrf] = override_staff
    payload = {"expected_version": item.version, "current_draft_id": str(draft.id)}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="https://testserver"
        ) as client:
            missing = await client.post(f"/api/v1/staff/email/{item.id}/approve", json=payload)
            first = await client.post(
                f"/api/v1/staff/email/{item.id}/approve",
                json=payload,
                headers={"Idempotency-Key": "approve-once"},
            )
            replay = await client.post(
                f"/api/v1/staff/email/{item.id}/approve",
                json=payload,
                headers={"Idempotency-Key": "approve-once"},
            )
    finally:
        application.dependency_overrides.clear()

    assert missing.status_code == 400
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert set(first.json()) == {"id", "state", "version", "current_draft_id"}
    assert first.json()["state"] == "SEND_PENDING"
    assert len(list((await db_session.scalars(select(EmailApproval))).all())) == 1
