from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from app.modules.authorization.models import ResourceGrant
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus


def test_principal_accepts_canonical_subject_id():
    subject_id = uuid4()

    principal = Principal(
        subject_id=subject_id,
        organization_id=uuid4(),
        email="canonical-principal@example.com",
        role=UserRole.MEMBER,
        session_id=uuid4(),
        csrf_hash="canonical-principal-csrf",
    )

    assert principal.subject_id == subject_id


async def _principal(db_session, *, role: UserRole = UserRole.MEMBER) -> Principal:
    organization = Organization(name=f"Policy organization {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"policy-subject-{uuid4()}",
        email=f"policy-{uuid4()}@example.com",
        role=role,
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    csrf_hash = sha256(b"policy-csrf").hexdigest()
    session = StaffSession(
        user_id=user.id,
        csrf_hash=csrf_hash,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    await db_session.flush()
    return Principal(
        subject_id=user.id,
        organization_id=organization.id,
        email=user.email,
        role=user.role,
        session_id=session.id,
        csrf_hash=csrf_hash,
    )


def _resource(principal: Principal, **changes: object) -> ResourceRef:
    values: dict[str, object] = {
        "organization_id": principal.organization_id,
        "resource_type": "knowledge",
        "resource_id": uuid4(),
        "state": ResourceState.ACTIVE,
        "is_public": False,
    }
    values.update(changes)
    return ResourceRef(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_same_organization_does_not_grant_unassigned_resource(db_session):
    principal = await _principal(db_session)
    resource = _resource(principal)

    with pytest.raises(AuthorizationDenied):
        await AuthorizationService(db_session).require(
            principal,
            "knowledge.read",
            resource,
        )


@pytest.mark.asyncio
async def test_matching_resource_grant_allows_role_action(db_session):
    principal = await _principal(db_session)
    resource = _resource(principal)
    db_session.add(
        ResourceGrant(
            organization_id=principal.organization_id,
            subject_id=principal.subject_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            actions=["knowledge.read"],
        )
    )
    await db_session.flush()

    await AuthorizationService(db_session).require(
        principal,
        "knowledge.read",
        resource,
    )


@pytest.mark.asyncio
async def test_role_matrix_denies_member_review_even_with_resource_grant(db_session):
    principal = await _principal(db_session, role=UserRole.MEMBER)
    resource = _resource(principal)
    db_session.add(
        ResourceGrant(
            organization_id=principal.organization_id,
            subject_id=principal.subject_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            actions=["knowledge.review"],
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationDenied):
        await AuthorizationService(db_session).require(
            principal,
            "knowledge.review",
            resource,
        )


@pytest.mark.asyncio
async def test_cross_organization_grant_does_not_authorize_resource(db_session):
    principal = await _principal(db_session, role=UserRole.ADMIN)
    foreign_principal = await _principal(db_session, role=UserRole.ADMIN)
    resource = _resource(foreign_principal)
    db_session.add(
        ResourceGrant(
            organization_id=foreign_principal.organization_id,
            subject_id=foreign_principal.subject_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            actions=["knowledge.read"],
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationDenied):
        await AuthorizationService(db_session).require(
            principal,
            "knowledge.read",
            resource,
        )


@pytest.mark.asyncio
async def test_disabled_resource_state_denies_assigned_action(db_session):
    principal = await _principal(db_session, role=UserRole.ADMIN)
    resource = _resource(principal, state=ResourceState.DISABLED)
    db_session.add(
        ResourceGrant(
            organization_id=principal.organization_id,
            subject_id=principal.subject_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            actions=["knowledge.read"],
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationDenied):
        await AuthorizationService(db_session).require(
            principal,
            "knowledge.read",
            resource,
        )


@pytest.mark.asyncio
async def test_active_public_resource_allows_read_without_assignment(db_session):
    principal = await _principal(db_session)
    resource = _resource(principal, is_public=True)

    await AuthorizationService(db_session).require(
        principal,
        "knowledge.read",
        resource,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "action"),
    [
        (UserRole.ADMIN, "knowledge.publish"),
        (UserRole.REVIEWER, "knowledge.review"),
        (UserRole.MEMBER, "knowledge.write"),
    ],
)
async def test_role_matrix_allows_each_roles_explicit_actions(db_session, role, action):
    principal = await _principal(db_session, role=role)
    resource = _resource(principal, state=ResourceState.DRAFT)
    db_session.add(
        ResourceGrant(
            organization_id=principal.organization_id,
            subject_id=principal.subject_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            actions=[action],
        )
    )
    await db_session.flush()

    await AuthorizationService(db_session).require(principal, action, resource)
