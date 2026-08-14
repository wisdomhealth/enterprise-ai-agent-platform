from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import StaffSession, StaffUser, UserStatus
from app.modules.identity.oidc import OIDCIdentity


class AdmissionDenied(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CreatedSession:
    id: UUID
    csrf_token: str
    expires_at: datetime


class IdentityService:
    def __init__(self, db_session: AsyncSession) -> None:
        self._db_session = db_session

    async def admit(self, identity: OIDCIdentity) -> StaffUser:
        if not identity.email_verified or not identity.subject or not identity.email:
            raise AdmissionDenied

        staff_users = (
            await self._db_session.scalars(
                select(StaffUser).where(StaffUser.email == identity.email).limit(2)
            )
        ).all()
        if len(staff_users) != 1:
            raise AdmissionDenied

        staff_user = staff_users[0]
        if staff_user.status is UserStatus.DISABLED:
            raise AdmissionDenied
        if staff_user.oidc_subject is not None and staff_user.oidc_subject != identity.subject:
            raise AdmissionDenied

        staff_user.oidc_subject = identity.subject
        if staff_user.status is UserStatus.INVITED:
            staff_user.status = UserStatus.ACTIVE
        await self._db_session.flush()
        return staff_user

    async def create_session(
        self,
        staff_user: StaffUser,
        *,
        ttl_seconds: int,
    ) -> CreatedSession:
        csrf_token = token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        staff_session = StaffSession(
            user_id=staff_user.id,
            csrf_hash=sha256(csrf_token.encode()).hexdigest(),
            expires_at=expires_at,
        )
        self._db_session.add(staff_session)
        await self._db_session.flush()
        return CreatedSession(
            id=staff_session.id,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )
