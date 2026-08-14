import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.authorization.models import ResourceGrant
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus

BACKEND_DIR = Path(__file__).resolve().parents[3]


def test_alembic_metadata_has_no_authorization_schema_drift():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=BACKEND_DIR,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.asyncio
async def test_database_rejects_grant_for_subject_from_another_organization(db_session):
    owner_organization = Organization(name="Grant owner organization")
    subject_organization = Organization(name="Grant subject organization")
    db_session.add_all([owner_organization, subject_organization])
    await db_session.flush()
    foreign_subject = StaffUser(
        organization_id=subject_organization.id,
        oidc_subject=f"foreign-grant-subject-{uuid4()}",
        email=f"foreign-grant-{uuid4()}@example.com",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(foreign_subject)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ResourceGrant(
                    organization_id=owner_organization.id,
                    subject_id=foreign_subject.id,
                    resource_type="knowledge",
                    resource_id=uuid4(),
                    actions=["knowledge.read"],
                )
            )
            await db_session.flush()
