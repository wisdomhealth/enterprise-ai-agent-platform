from collections.abc import Iterable
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import ConnectorKind
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import StaffUser, UserRole, UserStatus
from app.modules.operations.schemas import (
    ConnectorAuthorizationActionRead,
    ConnectorAuthorizationRead,
)
from app.modules.operations.service import OperationsNotFound

type ConnectorGrantAction = Literal[
    "connector.create", "connector.reauthorize", "connector.revoke"
]

CONNECTOR_GRANT_ACTIONS: tuple[ConnectorGrantAction, ...] = (
    "connector.create",
    "connector.reauthorize",
    "connector.revoke",
)


class ConnectorAuthorizationService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        audit_service: AuditService | None = None,
    ) -> None:
        self._db_session = db_session
        self._audit = audit_service or AuditService()

    async def list_for(
        self, principal: Principal, target_staff_user_id: UUID | None = None
    ) -> list[ConnectorAuthorizationRead]:
        target = await self._target(principal, target_staff_user_id or principal.subject_id)
        return await self._read(principal.organization_id, target.id)

    async def replace(
        self,
        principal: Principal,
        *,
        target_staff_user_id: UUID,
        kind: ConnectorKind,
        actions: Iterable[str],
    ) -> ConnectorAuthorizationRead:
        target = await self._target(principal, target_staff_user_id, lock_for_update=True)
        desired = set(actions)
        if not desired.issubset(CONNECTOR_GRANT_ACTIONS):
            raise ValueError("unsupported connector grant action")
        resource_id = ConnectorService.configuration_resource_id(principal.organization_id, kind)
        grant = await self._db_session.scalar(
            select(ResourceGrant)
            .where(
                ResourceGrant.organization_id == principal.organization_id,
                ResourceGrant.subject_id == target.id,
                ResourceGrant.resource_type == "connector",
                ResourceGrant.resource_id == resource_id,
            )
            .with_for_update()
        )
        existing = set(grant.actions) if grant is not None else set()
        added = sorted(desired - existing)
        removed = sorted(existing - desired)
        if grant is None and desired:
            self._db_session.add(
                ResourceGrant(
                    organization_id=principal.organization_id,
                    subject_id=target.id,
                    resource_type="connector",
                    resource_id=resource_id,
                    actions=sorted(desired),
                )
            )
        elif grant is not None and desired:
            grant.actions = sorted(desired)
        elif grant is not None:
            await self._db_session.delete(grant)
        if added or removed:
            await self._audit.record(
                self._db_session,
                principal,
                action="authorization.connector_grants.update",
                object_type="connector",
                object_id=resource_id,
                outcome="SUCCEEDED",
                details={
                    "target_staff_user_id": str(target.id),
                    "resource_type": "connector",
                    "connector_kind": kind.value,
                    "resource_id": str(resource_id),
                    "added_actions": added,
                    "removed_actions": removed,
                },
                safe_detail_keys={
                    "target_staff_user_id",
                    "resource_type",
                    "connector_kind",
                    "resource_id",
                    "added_actions",
                    "removed_actions",
                },
            )
        await self._db_session.flush()
        return self._read_one(target.id, kind, resource_id, desired)

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
            StaffUser.status == UserStatus.ACTIVE,
            StaffUser.role == UserRole.ADMIN,
        )
        if lock_for_update:
            statement = statement.with_for_update()
        target = await self._db_session.scalar(statement)
        if target is None:
            raise OperationsNotFound
        return target

    async def _read(
        self, organization_id: UUID, staff_user_id: UUID
    ) -> list[ConnectorAuthorizationRead]:
        resource_ids = {
            kind: ConnectorService.configuration_resource_id(organization_id, kind)
            for kind in ConnectorKind
        }
        grants = list(
            (
                await self._db_session.scalars(
                    select(ResourceGrant).where(
                        ResourceGrant.organization_id == organization_id,
                        ResourceGrant.subject_id == staff_user_id,
                        ResourceGrant.resource_type == "connector",
                        ResourceGrant.resource_id.in_(resource_ids.values()),
                    )
                )
            ).all()
        )
        actions_by_id = {grant.resource_id: set(grant.actions) for grant in grants}
        return [
            self._read_one(staff_user_id, kind, resource_id, actions_by_id.get(resource_id, set()))
            for kind, resource_id in sorted(resource_ids.items(), key=lambda item: item[0].value)
        ]

    @staticmethod
    def _read_one(
        staff_user_id: UUID, kind: ConnectorKind, resource_id: UUID, granted: set[str]
    ) -> ConnectorAuthorizationRead:
        return ConnectorAuthorizationRead(
            staff_user_id=staff_user_id,
            kind=kind,
            resource_id=resource_id,
            authorize_endpoint=f"/api/v1/admin/connectors/{kind.value}/authorize",
            actions=[
                ConnectorAuthorizationActionRead(action=action, granted=action in granted)
                for action in CONNECTOR_GRANT_ACTIONS
            ],
        )
