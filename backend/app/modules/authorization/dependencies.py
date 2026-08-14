from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import Action, ResourceRef
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session

ResourceLoader = Callable[..., Awaitable[ResourceRef | None]]
AuthorizationDependency = Callable[..., Awaitable[ResourceRef]]


def authorize(action: Action, resource_loader: ResourceLoader) -> AuthorizationDependency:
    async def authorized_resource(
        principal: Annotated[Principal, Depends(require_staff_session)],
        resource: Annotated[ResourceRef | None, Depends(resource_loader)],
        db_session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> ResourceRef:
        if resource is None or resource.organization_id != principal.organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        try:
            await AuthorizationService(db_session).require(principal, action, resource)
        except AuthorizationDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
        return resource

    return authorized_resource
