from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
from uuid import UUID

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.core.database import async_sessionmaker
from app.modules.identity.models import StaffSession, StaffUser, UserRole, UserStatus
from app.modules.identity.oidc import GoogleOIDCClient


@dataclass(frozen=True, slots=True)
class Principal:
    subject_id: UUID
    organization_id: UUID
    email: str
    role: UserRole
    session_id: UUID
    csrf_hash: str

    @property
    def id(self) -> UUID:
        """Compatibility alias for Task 3 consumers; new code uses subject_id."""

        return self.subject_id

    def organization_filter(
        self,
        organization_id: InstrumentedAttribute[UUID],
    ) -> ColumnElement[bool]:
        return organization_id == self.organization_id

    def subject_filter(self, subject_id: InstrumentedAttribute[UUID]) -> ColumnElement[bool]:
        return subject_id == self.subject_id


@dataclass(frozen=True, slots=True)
class ServicePrincipal(Principal):
    """Nonhuman principal restricted to one internal purpose and resource."""

    resource_type: str
    resource_id: UUID
    purpose: str


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker() as session:
        yield session


def get_oidc_client(request: Request) -> GoogleOIDCClient:
    oidc_client = getattr(request.app.state, "google_oidc_client", None)
    if not isinstance(oidc_client, GoogleOIDCClient):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OIDC is not configured",
        )
    return oidc_client


async def require_staff_session(
    staff_session: str | None = Cookie(default=None),
    db_session: AsyncSession = Depends(get_db_session),
) -> Principal:
    try:
        session_id = UUID(staff_session) if staff_session is not None else None
    except ValueError:
        session_id = None
    if session_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    row = (
        await db_session.execute(
            select(StaffSession, StaffUser)
            .join(StaffUser, StaffUser.id == StaffSession.user_id)
            .where(
                StaffSession.id == session_id,
                StaffSession.revoked_at.is_(None),
                StaffSession.expires_at > datetime.now(UTC),
                StaffUser.status == UserStatus.ACTIVE,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    session_record, staff_user = row
    return Principal(
        subject_id=staff_user.id,
        organization_id=staff_user.organization_id,
        email=staff_user.email,
        role=staff_user.role,
        session_id=session_record.id,
        csrf_hash=session_record.csrf_hash,
    )


async def require_staff_csrf(
    principal: Principal = Depends(require_staff_session),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Principal:
    supplied_hash = sha256((csrf_token or "").encode()).hexdigest()
    if csrf_token is None or not compare_digest(supplied_hash, principal.csrf_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return principal
