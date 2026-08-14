from collections.abc import Collection, Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.identity.dependencies import Principal


class AuditService:
    async def record(
        self,
        db_session: AsyncSession,
        principal: Principal,
        *,
        action: str,
        object_type: str,
        object_id: UUID,
        outcome: str,
        details: Mapping[str, object] | None = None,
        safe_detail_keys: Collection[str] = (),
    ) -> AuditEvent:
        return await self.record_actor(
            db_session,
            organization_id=principal.organization_id,
            actor_id=principal.subject_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            outcome=outcome,
            details=details,
            safe_detail_keys=safe_detail_keys,
        )

    async def record_actor(
        self,
        db_session: AsyncSession,
        *,
        organization_id: UUID,
        actor_id: UUID,
        action: str,
        object_type: str,
        object_id: UUID,
        outcome: str,
        details: Mapping[str, object] | None = None,
        safe_detail_keys: Collection[str] = (),
    ) -> AuditEvent:
        supplied_details = details or {}
        event = AuditEvent(
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            outcome=outcome,
            details={
                key: supplied_details[key]
                for key in safe_detail_keys
                if key in supplied_details
            },
        )
        db_session.add(event)
        await db_session.flush()
        return event
