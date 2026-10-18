from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.authorization.application import (
    ApplicationCapability,
    application_access_resource_id,
)
from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase
from app.modules.operations.schemas import ApplicationAccessRead
from app.modules.operations.service import OperationsNotFound


class ApplicationAuthorizationService:
    def __init__(self, db_session: AsyncSession) -> None:
        self._db_session = db_session
        self._audit = AuditService()

    async def list_for(
        self, principal: Principal, target_staff_user_id: UUID
    ) -> list[ApplicationAccessRead]:
        target = await self._target(principal, target_staff_user_id)
        return await self._read(target)

    async def replace(
        self,
        principal: Principal,
        *,
        target_staff_user_id: UUID,
        capability: ApplicationCapability,
        granted: bool,
        expected_user_version: int,
    ) -> ApplicationAccessRead:
        operator = await self._db_session.scalar(
            select(StaffUser)
            .where(
                StaffUser.id == principal.subject_id,
                StaffUser.organization_id == principal.organization_id,
                StaffUser.status == UserStatus.ACTIVE,
                StaffUser.role == UserRole.ADMIN,
            )
            .with_for_update()
        )
        if operator is None:
            raise OperationsNotFound
        target = await self._target(
            principal, target_staff_user_id, lock_for_update=True
        )
        if target.version != expected_user_version:
            raise ValueError("staff user version conflict")
        resource_id = application_access_resource_id(
            principal.organization_id, capability
        )
        grant = await self._db_session.scalar(
            select(ResourceGrant)
            .where(
                ResourceGrant.organization_id == principal.organization_id,
                ResourceGrant.subject_id == target.id,
                ResourceGrant.resource_type == "application",
                ResourceGrant.resource_id == resource_id,
            )
            .with_for_update()
        )
        changed = granted != (grant is not None)
        if granted and grant is None:
            self._db_session.add(
                ResourceGrant(
                    organization_id=principal.organization_id,
                    subject_id=target.id,
                    resource_type="application",
                    resource_id=resource_id,
                    actions=["application.access"],
                )
            )
        elif not granted and grant is not None:
            await self._db_session.delete(grant)
        if changed:
            target.version += 1
            await self._audit.record(
                self._db_session,
                principal,
                action="authorization.application_access.update",
                object_type="application",
                object_id=resource_id,
                outcome="SUCCEEDED",
                details={
                    "target_staff_user_id": str(target.id),
                    "capability": capability.value,
                    "granted": granted,
                },
                safe_detail_keys={"target_staff_user_id", "capability", "granted"},
            )
        await self._db_session.flush()
        return (await self._read(target, capabilities=(capability,)))[0]

    async def _target(
        self,
        principal: Principal,
        target_staff_user_id: UUID,
        *,
        lock_for_update: bool = False,
    ) -> StaffUser:
        statement = select(StaffUser).where(
            StaffUser.id == target_staff_user_id,
            StaffUser.organization_id == principal.organization_id,
        )
        if lock_for_update:
            statement = statement.with_for_update()
        target = await self._db_session.scalar(statement)
        if target is None:
            raise OperationsNotFound
        return target

    async def _read(
        self,
        target: StaffUser,
        *,
        capabilities: tuple[ApplicationCapability, ...] = tuple(ApplicationCapability),
    ) -> list[ApplicationAccessRead]:
        resource_ids = {
            capability: application_access_resource_id(
                target.organization_id, capability
            )
            for capability in capabilities
        }
        grants = set(
            (
                await self._db_session.scalars(
                    select(ResourceGrant.resource_id).where(
                        ResourceGrant.organization_id == target.organization_id,
                        ResourceGrant.subject_id == target.id,
                        ResourceGrant.resource_type == "application",
                        ResourceGrant.resource_id.in_(resource_ids.values()),
                        ResourceGrant.actions.contains(["application.access"]),
                    )
                )
            ).all()
        )
        knowledge_ids = list(
            (
                await self._db_session.scalars(
                    select(KnowledgeBase.id)
                    .join(
                        ResourceGrant,
                        (ResourceGrant.organization_id == KnowledgeBase.organization_id)
                        & (ResourceGrant.resource_id == KnowledgeBase.id),
                    )
                    .where(
                        KnowledgeBase.organization_id == target.organization_id,
                        ResourceGrant.subject_id == target.id,
                        ResourceGrant.resource_type == "knowledge",
                        ResourceGrant.actions.contains(["knowledge.review"]),
                    )
                )
            ).all()
        )
        eligible = target.status == UserStatus.ACTIVE and target.role in {
            UserRole.ADMIN,
            UserRole.REVIEWER,
        }
        return [
            ApplicationAccessRead(
                staff_user_id=target.id,
                capability=capability.value,
                resource_id=resource_id,
                grant_present=resource_id in grants,
                role_status_eligible=eligible,
                reviewable_knowledge_base_ids=knowledge_ids,
                effective_allowed=eligible and resource_id in grants and bool(knowledge_ids),
            )
            for capability, resource_id in resource_ids.items()
        ]
