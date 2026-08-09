import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.identity.models import (
    Organization,
    StaffUser,
    UserRole,
    UserStatus,
)


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
