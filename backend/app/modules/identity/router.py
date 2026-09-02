import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from authlib.integrations.base_client.errors import OAuthError  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.idempotency.models import IdempotencyState
from app.modules.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyLeaseLost,
    IdempotencyService,
)
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    get_oidc_client,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.identity.models import StaffSession, UserRole
from app.modules.identity.oidc import GoogleOIDCClient
from app.modules.identity.schemas import AdminUserRead, StaffInvitationCreate, StaffUserPatch
from app.modules.identity.service import (
    AdmissionDenied,
    IdentityManagementDenied,
    IdentityService,
    IdentityVersionConflict,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/v1/admin/users", tags=["administrator-users"])
ModelT = TypeVar("ModelT", bound=BaseModel)

SESSION_COOKIE = "staff_session"
CSRF_COOKIE = "staff_csrf"


def _callback_uri(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if settings.public_base_url is not None:
        return f"{str(settings.public_base_url).rstrip('/')}/api/v1/auth/callback"
    return str(request.url_for("auth_callback"))


@router.get("/login")
async def login(
    request: Request,
    oidc_client: GoogleOIDCClient = Depends(get_oidc_client),
) -> Response:
    if "session" not in request.scope:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC flow session storage is not configured",
        )
    return await oidc_client.authorization_redirect(request, _callback_uri(request))


@router.get("/callback", name="auth_callback")
async def callback(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    oidc_client: GoogleOIDCClient = Depends(get_oidc_client),
) -> Response:
    if "session" not in request.scope:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC flow session storage is not configured",
        )
    try:
        identity = await oidc_client.identity_from_callback(request)
        service = IdentityService(db_session)
        staff_user = await service.admit(identity)
        settings: Settings = request.app.state.settings
        created_session = await service.create_session(
            staff_user,
            ttl_seconds=settings.staff_session_ttl_seconds,
        )
        await db_session.commit()
    except (AdmissionDenied, OAuthError, ValueError) as exc:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    max_age = int((created_session.expires_at - datetime.now(UTC)).total_seconds())
    response.set_cookie(
        key=SESSION_COOKIE,
        value=str(created_session.id),
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=created_session.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/me")
async def me(principal: Principal = Depends(require_staff_session)) -> dict[str, str]:
    return {
        "id": str(principal.subject_id),
        "organization_id": str(principal.organization_id),
        "email": principal.email,
        "role": principal.role.value,
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    staff_session = await db_session.get(StaffSession, principal.session_id)
    if staff_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    staff_session.revoked_at = datetime.now(UTC)
    await db_session.commit()

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE,
        path="/",
        secure=True,
        samesite="lax",
    )
    return response


async def _require_admin_session(
    principal: Principal = Depends(require_staff_session),
) -> Principal:
    if principal.role is not UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return principal


async def _require_admin_csrf(
    principal: Principal = Depends(require_staff_csrf),
) -> Principal:
    if principal.role is not UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return principal


def _write_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is required",
        )
    return value.strip()


async def _idempotent_user_action(
    *,
    db_session: AsyncSession,
    principal: Principal,
    key: str,
    operation: str,
    object_id: UUID,
    request_body: dict[str, object],
    invoke: Callable[[], Awaitable[AdminUserRead]],
) -> AdminUserRead:
    idempotency = IdempotencyService(db_session)
    try:
        record = await idempotency.begin(
            scope_id=principal.organization_id,
            actor_id=principal.subject_id,
            operation=operation,
            object_id=object_id,
            key=key,
            request_hash=sha256(
                json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        if record.state is IdempotencyState.COMPLETED:
            return AdminUserRead.model_validate(record.response_body)
        response = await invoke()
        body = response.model_dump(mode="json")
        await idempotency.complete(
            record.id,
            status.HTTP_200_OK,
            body,
            lease_token=record.lease_token,
            safe_response_keys=set(body),
        )
        await db_session.commit()
        return response
    except (IdempotencyConflict, IdempotencyInProgress, IdempotencyLeaseLost):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from None
    except IdentityManagementDenied:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except IdentityVersionConflict as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESOURCE_VERSION_CONFLICT",
                "state": error.state.value,
                "version": error.version,
            },
        ) from None
    except LookupError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from None


@admin_router.get("", response_model=list[AdminUserRead])
async def list_staff_users(
    principal: Annotated[Principal, Depends(_require_admin_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[AdminUserRead]:
    users = await IdentityService(db_session).list_staff(principal)
    return [AdminUserRead.model_validate(user) for user in users]


@admin_router.post(
    "/invitations",
    response_model=AdminUserRead,
    status_code=status.HTTP_201_CREATED,
)
async def invite_staff_user(
    payload: StaffInvitationCreate,
    principal: Annotated[Principal, Depends(_require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> AdminUserRead:
    service = IdentityService(db_session)
    object_id = uuid5(
        NAMESPACE_URL,
        f"staff-invitation:{principal.organization_id}:{payload.email.strip().lower()}",
    )
    return await _idempotent_user_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.user.invite",
        object_id=object_id,
        request_body=payload.model_dump(mode="json"),
        invoke=lambda: _invite_read(service, principal, payload),
    )


async def _invite_read(
    service: IdentityService,
    principal: Principal,
    payload: StaffInvitationCreate,
) -> AdminUserRead:
    user = await service.invite(principal, email=payload.email, role=payload.role)
    return AdminUserRead.model_validate(user)


@admin_router.patch("/{user_id}", response_model=AdminUserRead)
async def update_staff_user(
    user_id: UUID,
    payload: StaffUserPatch,
    principal: Annotated[Principal, Depends(_require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> AdminUserRead:
    service = IdentityService(db_session)
    return await _idempotent_user_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.user.update",
        object_id=user_id,
        request_body=payload.model_dump(mode="json"),
        invoke=lambda: _update_user_read(service, principal, user_id, payload),
    )


async def _update_user_read(
    service: IdentityService,
    principal: Principal,
    user_id: UUID,
    payload: StaffUserPatch,
) -> AdminUserRead:
    user = await service.update_staff(
        principal,
        user_id,
        expected_version=payload.expected_version,
        role=payload.role,
        status=payload.status,
    )
    return AdminUserRead.model_validate(user)
