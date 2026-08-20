from types import MappingProxyType
from typing import Final

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.modules.authorization.models import ResourceGrant
from app.modules.authorization.types import Action, ResourceRef, ResourceState
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole


class AuthorizationDenied(Exception):
    pass


ROLE_ACTIONS: Final = MappingProxyType(
    {
        UserRole.ADMIN: frozenset(
            {
                "connector.create",
                "connector.reauthorize",
                "connector.revoke",
                "knowledge.read",
                "knowledge.write",
                "knowledge.review",
                "knowledge.publish",
            }
        ),
        UserRole.REVIEWER: frozenset({"knowledge.read", "knowledge.review"}),
        UserRole.MEMBER: frozenset({"knowledge.read", "knowledge.write"}),
    }
)

ACTION_STATES: Final = MappingProxyType(
    {
        "connector.create": frozenset({ResourceState.ACTIVE}),
        "connector.reauthorize": frozenset({ResourceState.REAUTH_REQUIRED, ResourceState.ERROR}),
        "connector.revoke": frozenset({ResourceState.ACTIVE}),
        "knowledge.read": frozenset(
            {ResourceState.DRAFT, ResourceState.ACTIVE, ResourceState.ARCHIVED}
        ),
        "knowledge.write": frozenset({ResourceState.DRAFT, ResourceState.ACTIVE}),
        "knowledge.review": frozenset({ResourceState.DRAFT, ResourceState.ACTIVE}),
        "knowledge.publish": frozenset({ResourceState.DRAFT}),
    }
)


def resource_grant_filter(principal: Principal) -> ColumnElement[bool]:
    """Scope candidate and list queries to grants visible to one principal."""

    return and_(
        principal.organization_filter(ResourceGrant.organization_id),
        principal.subject_filter(ResourceGrant.subject_id),
    )


class AuthorizationService:
    def __init__(self, db_session: AsyncSession) -> None:
        self._db_session = db_session

    async def is_visible(self, principal: Principal, resource: ResourceRef) -> bool:
        if resource.organization_id != principal.organization_id:
            return False
        if resource.is_public and resource.state is ResourceState.ACTIVE:
            return True
        grant_id = await self._db_session.scalar(
            select(ResourceGrant.id).where(
                resource_grant_filter(principal),
                ResourceGrant.resource_type == resource.resource_type,
                ResourceGrant.resource_id == resource.resource_id,
            )
        )
        return grant_id is not None

    async def require(
        self,
        principal: Principal,
        action: Action,
        resource: ResourceRef,
    ) -> None:
        if resource.organization_id != principal.organization_id:
            raise AuthorizationDenied
        if action not in ROLE_ACTIONS[principal.role]:
            raise AuthorizationDenied
        if resource.state not in ACTION_STATES[action]:
            raise AuthorizationDenied
        if (
            resource.is_public
            and action == "knowledge.read"
            and resource.state is ResourceState.ACTIVE
        ):
            return

        grant_id = await self._db_session.scalar(
            select(ResourceGrant.id).where(
                resource_grant_filter(principal),
                ResourceGrant.resource_type == resource.resource_type,
                ResourceGrant.resource_id == resource.resource_id,
                ResourceGrant.actions.contains([action]),
            )
        )
        if grant_id is None:
            raise AuthorizationDenied
