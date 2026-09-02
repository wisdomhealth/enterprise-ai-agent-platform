from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import StaffSession, StaffUser, UserRole, UserStatus
from app.modules.identity.oidc import OIDCIdentity
from app.modules.outbox.service import OutboxService


class AdmissionDenied(Exception):
    pass


class IdentityManagementDenied(Exception):
    pass


class IdentityVersionConflict(Exception):
    def __init__(self, user: StaffUser) -> None:
        self.state = user.status
        self.version = user.version


@dataclass(frozen=True, slots=True)
class CreatedSession:
    id: UUID
    csrf_token: str
    expires_at: datetime


class IdentityService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()

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

        updated_users = (
            await self._db_session.scalars(
                update(StaffUser)
                .where(
                    StaffUser.id == staff_user.id,
                    StaffUser.email == identity.email,
                    StaffUser.status.in_([UserStatus.INVITED, UserStatus.ACTIVE]),
                    or_(
                        StaffUser.oidc_subject.is_(None),
                        StaffUser.oidc_subject == identity.subject,
                    ),
                )
                .values(
                    oidc_subject=identity.subject,
                    status=UserStatus.ACTIVE,
                )
                .returning(StaffUser)
            )
        ).all()
        if len(updated_users) != 1:
            raise AdmissionDenied

        admitted_user = updated_users[0]
        admitted_user.oidc_subject = identity.subject
        admitted_user.status = UserStatus.ACTIVE
        await self._db_session.flush()
        return admitted_user

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

    async def list_staff(self, principal: Principal) -> list[StaffUser]:
        self._require_admin(principal)
        return list(
            (
                await self._db_session.scalars(
                    select(StaffUser)
                    .where(StaffUser.organization_id == principal.organization_id)
                    .order_by(StaffUser.email, StaffUser.id)
                )
            ).all()
        )

    async def invite(self, principal: Principal, *, email: str, role: UserRole) -> StaffUser:
        self._require_admin(principal)
        normalized_email = email.strip().lower()
        if "@" not in normalized_email or normalized_email.startswith("@"):
            raise ValueError("valid invitation email is required")
        await self._db_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"staff-invite:{principal.organization_id}:{normalized_email}"},
        )
        existing = await self._db_session.scalar(
            select(StaffUser).where(
                StaffUser.organization_id == principal.organization_id,
                func.lower(StaffUser.email) == normalized_email,
            )
        )
        if existing is not None:
            raise ValueError("staff invitation already exists")
        user = StaffUser(
            organization_id=principal.organization_id,
            oidc_subject=None,
            email=normalized_email,
            role=role,
            status=UserStatus.INVITED,
        )
        self._db_session.add(user)
        await self._db_session.flush()
        await self._record_user_event(
            principal,
            user,
            action="identity.user.invite",
            event_type="identity.user.invited",
            previous=None,
        )
        return user

    async def update_staff(
        self,
        principal: Principal,
        user_id: UUID,
        *,
        expected_version: int,
        role: UserRole | None,
        status: UserStatus | None,
    ) -> StaffUser:
        self._require_admin(principal)
        user = await self._db_session.scalar(
            select(StaffUser)
            .where(
                StaffUser.id == user_id,
                StaffUser.organization_id == principal.organization_id,
            )
            .with_for_update()
        )
        if user is None:
            raise LookupError(user_id)
        if user.version != expected_version:
            raise IdentityVersionConflict(user)
        previous: dict[str, object] = {
            "role": user.role.value,
            "status": user.status.value,
            "version": user.version,
        }
        if role is not None:
            user.role = role
        if status is not None:
            user.status = status
        user.version += 1
        if status is UserStatus.DISABLED:
            await self._db_session.execute(
                update(StaffSession)
                .where(
                    StaffSession.user_id == user.id,
                    StaffSession.revoked_at.is_(None),
                )
                .values(revoked_at=func.clock_timestamp())
            )
        await self._db_session.flush()
        await self._record_user_event(
            principal,
            user,
            action="identity.user.update",
            event_type="identity.user.updated",
            previous=previous,
        )
        return user

    @staticmethod
    def _require_admin(principal: Principal) -> None:
        if principal.role is not UserRole.ADMIN:
            raise IdentityManagementDenied

    async def _record_user_event(
        self,
        principal: Principal,
        user: StaffUser,
        *,
        action: str,
        event_type: str,
        previous: dict[str, object] | None,
    ) -> None:
        details: dict[str, object] = {
            "role": user.role.value,
            "status": user.status.value,
            "version": user.version,
        }
        if previous is not None:
            details["previous"] = previous
        await self._audit.record(
            self._db_session,
            principal,
            action=action,
            object_type="staff_user",
            object_id=user.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            event_type,
            "staff_user",
            user.id,
            {"organization_id": str(user.organization_id), **details},
        )
