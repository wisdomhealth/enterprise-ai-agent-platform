from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import Action, ResourceRef
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session

AuthorizationDependency = Callable[..., Awaitable[ResourceRef]]


class ResourceLoader(Protocol):
    async def __call__(
        self,
        resource_id: UUID,
        principal: Principal,
        db_session: AsyncSession,
    ) -> ResourceRef | None: ...


def authorize(action: Action, resource_loader: ResourceLoader) -> AuthorizationDependency:
    async def authorized_resource(
        resource_id: UUID,
        principal: Annotated[Principal, Depends(require_staff_session)],
        db_session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> ResourceRef:
        resource = await resource_loader(resource_id, principal, db_session)
        if resource is None or resource.organization_id != principal.organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        authorization_service = AuthorizationService(db_session)
        if not await authorization_service.is_visible(principal, resource):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        try:
            await authorization_service.require(principal, action, resource)
        except AuthorizationDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        return resource

    return authorized_resource
