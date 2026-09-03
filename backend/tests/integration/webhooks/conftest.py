from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.encryption import EnvelopeCipher, FileKeyWrapper
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus


@pytest_asyncio.fixture
async def webhook_context(db_session: AsyncSession, tmp_path: Path) -> dict[str, object]:
    organization = Organization(name=f"Webhook {uuid4()}")
    foreign_organization = Organization(name=f"Foreign webhook {uuid4()}")
    db_session.add_all((organization, foreign_organization))
    await db_session.flush()
    admin = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"webhook-admin-{uuid4()}",
        email="webhook-admin@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    member = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"webhook-member-{uuid4()}",
        email="webhook-member@example.test",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    foreign_admin = StaffUser(
        organization_id=foreign_organization.id,
        oidc_subject=f"foreign-webhook-admin-{uuid4()}",
        email="foreign-webhook-admin@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db_session.add_all((admin, member, foreign_admin))
    await db_session.flush()
    key_path = tmp_path / "webhook.key"
    key_path.write_bytes(b"k" * 32)

    def principal(user: StaffUser) -> Principal:
        return Principal(
            subject_id=user.id,
            organization_id=user.organization_id,
            email=user.email,
            role=user.role,
            session_id=uuid4(),
            csrf_hash="csrf",
        )

    return {
        "organization": organization,
        "admin": admin,
        "member": member,
        "foreign_admin": foreign_admin,
        "principal": principal,
        "cipher": EnvelopeCipher(FileKeyWrapper(key_path)),
    }
