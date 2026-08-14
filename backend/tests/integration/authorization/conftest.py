from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.authorization.dependencies import authorize
from app.modules.authorization.models import ResourceGrant
from app.modules.authorization.policy import resource_grant_filter
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.identity.dependencies import Principal, get_db_session
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus

PROBE_RESOURCE_TYPE = "knowledge"


async def load_probe_resource(
    resource_id: UUID,
    principal: Principal,
    db_session: AsyncSession,
) -> ResourceRef | None:
    grant = await db_session.scalar(
        select(ResourceGrant).where(
            resource_grant_filter(principal),
            ResourceGrant.resource_type == PROBE_RESOURCE_TYPE,
            ResourceGrant.resource_id == resource_id,
        )
    )
    if grant is None:
        return None
    return ResourceRef(
        organization_id=grant.organization_id,
        resource_type=grant.resource_type,
        resource_id=grant.resource_id,
        state=ResourceState.ACTIVE,
    )


async def load_unfiltered_probe_resource(
    resource_id: UUID,
    _principal: Principal,
    db_session: AsyncSession,
) -> ResourceRef | None:
    grant = await db_session.scalar(
        select(ResourceGrant).where(ResourceGrant.resource_id == resource_id)
    )
    if grant is None:
        return None
    return ResourceRef(
        organization_id=grant.organization_id,
        resource_type=grant.resource_type,
        resource_id=grant.resource_id,
        state=ResourceState.ACTIVE,
    )


probe_router = APIRouter(prefix="/api/v1/authorization-probe")


@probe_router.get("/{resource_id}")
async def authorization_probe(
    resource: ResourceRef = Depends(authorize("knowledge.read", load_probe_resource)),
) -> dict[str, str]:
    return {"resource_id": str(resource.resource_id)}


@probe_router.get("/unfiltered/{resource_id}")
async def unfiltered_authorization_probe(
    resource: ResourceRef = Depends(
        authorize("knowledge.read", load_unfiltered_probe_resource)
    ),
) -> dict[str, str]:
    return {"resource_id": str(resource.resource_id)}


@pytest.fixture
def app(db_session: AsyncSession) -> FastAPI:
    application = create_app(Settings.model_validate({"SESSION_SECRET": "probe-secret"}))
    application.include_router(probe_router)

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = override_db_session
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


async def create_staff_session(
    db_session: AsyncSession,
    organization: Organization,
    *,
    email: str,
    role: UserRole = UserRole.MEMBER,
) -> tuple[StaffUser, str]:
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"probe-subject-{uuid4()}",
        email=email,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    session = StaffSession(
        user_id=user.id,
        csrf_hash=sha256(b"probe-csrf").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    await db_session.flush()
    return user, str(session.id)


@pytest_asyncio.fixture
async def authorization_records(db_session: AsyncSession) -> dict[str, object]:
    staff_organization = Organization(name="Probe Staff")
    foreign_organization = Organization(name="Probe Foreign")
    db_session.add_all([staff_organization, foreign_organization])
    await db_session.flush()
    staff_user, staff_cookie = await create_staff_session(
        db_session,
        staff_organization,
        email="probe-staff@example.com",
    )
    foreign_user, _ = await create_staff_session(
        db_session,
        foreign_organization,
        email="probe-foreign@example.com",
    )
    other_staff_user, _ = await create_staff_session(
        db_session,
        staff_organization,
        email="probe-other-staff@example.com",
    )

    readable_resource_id = uuid4()
    forbidden_resource_id = uuid4()
    foreign_resource_id = uuid4()
    other_subject_resource_id = uuid4()
    db_session.add_all(
        [
            ResourceGrant(
                organization_id=staff_organization.id,
                subject_id=staff_user.id,
                resource_type=PROBE_RESOURCE_TYPE,
                resource_id=readable_resource_id,
                actions=["knowledge.read"],
            ),
            ResourceGrant(
                organization_id=staff_organization.id,
                subject_id=staff_user.id,
                resource_type=PROBE_RESOURCE_TYPE,
                resource_id=forbidden_resource_id,
                actions=["knowledge.review"],
            ),
            ResourceGrant(
                organization_id=foreign_organization.id,
                subject_id=foreign_user.id,
                resource_type=PROBE_RESOURCE_TYPE,
                resource_id=foreign_resource_id,
                actions=["knowledge.read"],
            ),
            ResourceGrant(
                organization_id=staff_organization.id,
                subject_id=other_staff_user.id,
                resource_type=PROBE_RESOURCE_TYPE,
                resource_id=other_subject_resource_id,
                actions=["knowledge.read"],
            ),
        ]
    )
    await db_session.flush()
    return {
        "staff_cookie": staff_cookie,
        "staff_user": staff_user,
        "readable_resource_id": readable_resource_id,
        "forbidden_resource_id": forbidden_resource_id,
        "foreign_resource_id": foreign_resource_id,
        "other_subject_resource_id": other_subject_resource_id,
    }
