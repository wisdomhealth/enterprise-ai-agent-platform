from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.application import (
    ApplicationCapability,
    application_access_resource_id,
)
from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_session,
)
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase
from app.modules.operations.application_authorization import ApplicationAuthorizationService
from app.modules.operations.router import staff_router


@pytest.mark.asyncio
async def test_application_grant_does_not_replace_role_status_or_knowledge_scope(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Application access {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"application-{uuid4().hex}"
    )
    admin = StaffUser(
        organization_id=organization.id,
        email="admin@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    reviewer = StaffUser(
        organization_id=organization.id,
        email="reviewer@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    member = StaffUser(
        organization_id=organization.id,
        email="member@example.test",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    db_session.add_all([knowledge_base, admin, reviewer, member])
    await db_session.flush()
    db_session.add_all(
        [
            ResourceGrant(
                organization_id=organization.id,
                subject_id=user.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.review"],
            )
            for user in (reviewer, member)
        ]
    )
    await db_session.flush()
    principal = Principal(
        admin.id, organization.id, admin.email, admin.role, uuid4(), "csrf"
    )
    service = ApplicationAuthorizationService(db_session)

    reviewer_access = await service.replace(
        principal,
        target_staff_user_id=reviewer.id,
        capability=ApplicationCapability.EMAIL,
        granted=True,
        expected_user_version=reviewer.version,
    )
    member_access = await service.replace(
        principal,
        target_staff_user_id=member.id,
        capability=ApplicationCapability.EMAIL,
        granted=True,
        expected_user_version=member.version,
    )

    assert reviewer_access.grant_present is True
    assert reviewer_access.reviewable_knowledge_base_ids == [knowledge_base.id]
    assert reviewer_access.effective_allowed is True
    assert member_access.grant_present is True
    assert member_access.role_status_eligible is False
    assert member_access.effective_allowed is False


@pytest.mark.asyncio
async def test_staff_can_read_only_their_own_effective_application_access(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Self application access {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"self-access-{uuid4().hex}"
    )
    reviewer = StaffUser(
        organization_id=organization.id,
        email="self-reviewer@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add_all([knowledge_base, reviewer])
    await db_session.flush()
    principal = Principal(
        reviewer.id,
        organization.id,
        reviewer.email,
        reviewer.role,
        uuid4(),
        "csrf",
    )
    db_session.add_all(
        [
            ResourceGrant(
                organization_id=organization.id,
                subject_id=reviewer.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.review"],
            ),
            ResourceGrant(
                organization_id=organization.id,
                subject_id=reviewer.id,
                resource_type="application",
                resource_id=application_access_resource_id(
                    organization.id, ApplicationCapability.EMAIL
                ),
                actions=["application.access"],
            ),
        ]
    )
    await db_session.flush()

    app = FastAPI()
    app.include_router(staff_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_staff_session] = lambda: principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/api/v1/staff/application-access")

    assert response.status_code == 200
    assert response.json() == [
        {
            "staff_user_id": str(reviewer.id),
            "capability": "email",
            "resource_id": str(
                application_access_resource_id(
                    organization.id, ApplicationCapability.EMAIL
                )
            ),
            "grant_present": True,
            "role_status_eligible": True,
            "reviewable_knowledge_base_ids": [str(knowledge_base.id)],
            "effective_allowed": True,
        },
        {
            "staff_user_id": str(reviewer.id),
            "capability": "support",
            "resource_id": str(
                application_access_resource_id(
                    organization.id, ApplicationCapability.SUPPORT
                )
            ),
            "grant_present": False,
            "role_status_eligible": True,
            "reviewable_knowledge_base_ids": [str(knowledge_base.id)],
            "effective_allowed": False,
        },
    ]
