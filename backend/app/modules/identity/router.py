from datetime import UTC, datetime

from authlib.integrations.base_client.errors import OAuthError  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    get_oidc_client,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.identity.models import StaffSession
from app.modules.identity.oidc import GoogleOIDCClient
from app.modules.identity.service import AdmissionDenied, IdentityService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

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
