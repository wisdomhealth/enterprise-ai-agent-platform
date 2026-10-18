from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session


class ApplicationCapability(StrEnum):
    EMAIL = "email"
    SUPPORT = "support"


def application_access_resource_id(
    organization_id: UUID,
    capability: ApplicationCapability,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"application-access:{organization_id}:{capability.value}",
    )


def application_access_resource(
    organization_id: UUID,
    capability: ApplicationCapability,
) -> ResourceRef:
    return ResourceRef(
        organization_id=organization_id,
        resource_type="application",
        resource_id=application_access_resource_id(organization_id, capability),
        state=ResourceState.ACTIVE,
    )


async def require_application_access(
    capability: ApplicationCapability,
    principal: Principal,
    db_session: AsyncSession,
) -> Principal:
    try:
        await AuthorizationService(db_session).require(
            principal,
            "application.access",
            application_access_resource(principal.organization_id, capability),
        )
    except AuthorizationDenied:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
    return principal


async def require_email_application_access(
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Principal:
    return await require_application_access(
        ApplicationCapability.EMAIL, principal, db_session
    )


async def require_support_application_access(
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Principal:
    return await require_application_access(
        ApplicationCapability.SUPPORT, principal, db_session
    )


async def require_email_or_support_application_access(
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> Principal:
    for capability in (ApplicationCapability.EMAIL, ApplicationCapability.SUPPORT):
        try:
            await AuthorizationService(db_session).require(
                principal,
                "application.access",
                application_access_resource(principal.organization_id, capability),
            )
            return principal
        except AuthorizationDenied:
            continue
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
