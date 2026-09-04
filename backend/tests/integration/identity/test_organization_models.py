from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker as create_async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database import engine
from app.modules.identity.models import (
    Organization,
    StaffUser,
    UserRole,
    UserStatus,
)

_COMMITTED_ORGANIZATION_NAME = f"fixture-leak-probe-{uuid4()}"


@pytest.mark.asyncio
async def test_db_fixture_allows_commits(db_session):
    organization = Organization(name=_COMMITTED_ORGANIZATION_NAME)
    db_session.add(organization)

    await db_session.commit()

    assert organization.id is not None


@pytest.mark.asyncio
async def test_db_fixture_rolls_back_records_committed_by_the_previous_test():
    independent_engine = create_async_engine(engine.url, poolclass=NullPool)
    independent_sessionmaker = create_async_sessionmaker(independent_engine)
    try:
        async with independent_sessionmaker() as independent_session:
            organization_id = await independent_session.scalar(
                select(Organization.id).where(
                    Organization.name == _COMMITTED_ORGANIZATION_NAME
                )
            )
    finally:
        await independent_engine.dispose()

    assert organization_id is None


@pytest.mark.asyncio
async def test_staff_identity_is_unique_inside_an_organization(db_session):
    organization = Organization(name="Acme")
    db_session.add(organization)
    await db_session.flush()
    db_session.add_all(
        [
            StaffUser(
                organization_id=organization.id,
                oidc_subject="google-subject-1",
                email="agent@example.com",
                role=UserRole.REVIEWER,
                status=UserStatus.ACTIVE,
            ),
            StaffUser(
                organization_id=organization.id,
                oidc_subject="google-subject-1",
                email="other@example.com",
                role=UserRole.MEMBER,
                status=UserStatus.ACTIVE,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_identity_records_use_uuid_version_and_utc_defaults(db_session):
    organization = Organization(name="Defaults")
    db_session.add(organization)
    await db_session.flush()
    staff_user = StaffUser(
        organization_id=organization.id,
        oidc_subject="defaults-subject",
        email="defaults@example.com",
        role=UserRole.ADMIN,
        status=UserStatus.INVITED,
    )
    db_session.add(staff_user)

    await db_session.flush()

    assert isinstance(organization.id, UUID)
    assert isinstance(staff_user.id, UUID)
    assert staff_user.version == 1
    assert organization.created_at.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_staff_identity_can_repeat_across_organizations(db_session):
    organizations = [Organization(name="Acme"), Organization(name="Globex")]
    db_session.add_all(organizations)
    await db_session.flush()
    db_session.add_all(
        [
            StaffUser(
                organization_id=organizations[0].id,
                oidc_subject="shared-subject",
                email="acme@example.com",
                role=UserRole.REVIEWER,
                status=UserStatus.ACTIVE,
            ),
            StaffUser(
                organization_id=organizations[1].id,
                oidc_subject="shared-subject",
                email="globex@example.com",
                role=UserRole.MEMBER,
                status=UserStatus.ACTIVE,
            ),
        ]
    )

    await db_session.commit()

    staff_user_ids = (
        await db_session.scalars(
            select(StaffUser.id).where(StaffUser.oidc_subject == "shared-subject")
        )
    ).all()
    assert len(staff_user_ids) == 2


@pytest.mark.asyncio
async def test_deleting_an_organization_cascades_to_its_staff(db_session):
    organization = Organization(name="Delete Me")
    db_session.add(organization)
    await db_session.flush()
    staff_user = StaffUser(
        organization_id=organization.id,
        oidc_subject="delete-subject",
        email="delete@example.com",
        role=UserRole.MEMBER,
        status=UserStatus.DISABLED,
    )
    db_session.add(staff_user)
    await db_session.flush()
    staff_user_id = staff_user.id

    await db_session.delete(organization)
    await db_session.flush()

    assert await db_session.scalar(
        select(StaffUser.id).where(StaffUser.id == staff_user_id)
    ) is None
