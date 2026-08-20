import secrets
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.connectors.models import ConnectorKind
from app.modules.connectors.schemas import ConnectorRead
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)

router = APIRouter(prefix="/api/v1/admin/connectors", tags=["connectors"])

_GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPES = {
    ConnectorKind.DRIVE: ("https://www.googleapis.com/auth/drive.readonly",),
    ConnectorKind.GMAIL: (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ),
}


def _connector_service(request: Request) -> ConnectorService:
    service = getattr(request.app.state, "connector_service", None)
    if not isinstance(service, ConnectorService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="connector encryption is not configured",
        )
    return service


def _callback_uri(request: Request, kind: ConnectorKind) -> str:
    settings: Settings = request.app.state.settings
    if settings.public_base_url is not None:
        base_url = str(settings.public_base_url).rstrip("/")
        return f"{base_url}/api/v1/admin/connectors/{kind.value}/callback"
    return str(request.url_for("connector_callback", kind=kind.value))


def _client_credentials(settings: Settings, kind: ConnectorKind) -> tuple[str, str]:
    if kind is ConnectorKind.DRIVE:
        client_id, client_secret = (
            settings.google_drive_client_id,
            settings.google_drive_client_secret,
        )
    else:
        client_id, client_secret = (
            settings.google_gmail_client_id,
            settings.google_gmail_client_secret,
        )
    if client_id is None or client_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google connector OAuth is not configured",
        )
    return client_id.get_secret_value(), client_secret.get_secret_value()


def _require_same_origin_oauth_start(request: Request) -> None:
    fetch_site = request.headers.get("sec-fetch-site")
    supplied_origin = request.headers.get("origin")
    if fetch_site in {"cross-site", "none", "same-site"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="cross-site OAuth start denied"
        )
    if fetch_site == "same-origin":
        return
    if supplied_origin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="unproven OAuth start origin denied"
        )
    settings: Settings = request.app.state.settings
    expected_origin = (
        str(settings.public_base_url).rstrip("/")
        if settings.public_base_url is not None
        else str(request.base_url).rstrip("/")
    )
    if supplied_origin.rstrip("/") != expected_origin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="cross-origin OAuth start denied"
        )


async def _exchange_code(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str
) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Google token exchange failed"
        )
    refresh_token = response.json().get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Google did not return a refresh token"
        )
    return refresh_token


@router.get("/{kind}/authorize")
async def authorize(
    kind: ConnectorKind,
    request: Request,
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
    service: ConnectorService = Depends(_connector_service),
) -> RedirectResponse:
    _require_same_origin_oauth_start(request)
    await service.require_authorization_start(db_session, principal=principal, kind=kind)
    if "session" not in request.scope:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    client_id, _ = _client_credentials(request.app.state.settings, kind)
    state = secrets.token_urlsafe(32)
    request.session["connector_oauth"] = {"state": state, "kind": kind.value}
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": _callback_uri(request, kind),
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "scope": " ".join(_SCOPES[kind]),
            "state": state,
        }
    )
    return RedirectResponse(
        f"{_GOOGLE_AUTHORIZATION_URL}?{params}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{kind}/callback", name="connector_callback")
async def callback(
    kind: ConnectorKind,
    request: Request,
    code: str,
    state: str,
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
    service: ConnectorService = Depends(_connector_service),
) -> ConnectorRead:
    expected = request.session.pop("connector_oauth", None) if "session" in request.scope else None
    if (
        not isinstance(expected, dict)
        or expected.get("state") != state
        or expected.get("kind") != kind.value
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid OAuth state")
    client_id, client_secret = _client_credentials(request.app.state.settings, kind)
    refresh_token = await _exchange_code(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_callback_uri(request, kind),
    )
    connector = await service.create_or_reauthorize(
        db_session, principal=principal, kind=kind, refresh_token=refresh_token
    )
    await db_session.commit()
    return ConnectorRead(id=str(connector.id), kind=connector.kind, status=connector.status)


@router.post("/{connector_id}/revoke")
async def revoke(
    connector_id: UUID,
    db_session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_staff_csrf),
    service: ConnectorService = Depends(_connector_service),
) -> ConnectorRead:
    connector = await service.revoke(db_session, principal=principal, connector_id=connector_id)
    await db_session.commit()
    return ConnectorRead(id=str(connector.id), kind=connector.kind, status=connector.status)
