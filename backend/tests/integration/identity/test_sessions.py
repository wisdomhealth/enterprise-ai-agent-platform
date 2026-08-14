from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.identity.dependencies import get_db_session, get_oidc_client
from app.modules.identity.models import (
    Organization,
    StaffSession,
    StaffUser,
    UserRole,
    UserStatus,
)
from app.modules.identity.oidc import OIDCIdentity


@pytest.fixture
def app(db_session: AsyncSession) -> FastAPI:
    application = create_app()

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = override_db_session
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
    ) as test_client:
        yield test_client


async def create_user_and_session(
    db_session: AsyncSession,
    *,
    status: UserStatus = UserStatus.ACTIVE,
    revoked: bool = False,
    expired: bool = False,
) -> tuple[StaffUser, str, str]:
    organization = Organization(name="Session Test")
    db_session.add(organization)
    await db_session.flush()
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject="google-session-subject",
        email="session@example.com",
        role=UserRole.MEMBER,
        status=status,
    )
    db_session.add(user)
    await db_session.flush()
    csrf_token = "csrf-token-known-only-to-the-browser"
    staff_session = StaffSession(
        user_id=user.id,
        csrf_hash=sha256(csrf_token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + (timedelta(minutes=-1) if expired else timedelta(hours=1)),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    db_session.add(staff_session)
    await db_session.flush()
    return user, str(staff_session.id), csrf_token


@pytest.mark.asyncio
async def test_valid_server_session_returns_staff_principal(client, db_session):
    user, session_cookie, _ = await create_user_and_session(db_session)
    client.cookies.set("staff_session", session_cookie)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "organization_id": str(user.organization_id),
        "email": "session@example.com",
        "role": "MEMBER",
    }


@pytest.mark.asyncio
async def test_disabled_user_session_is_rejected(client, db_session):
    _, disabled_session_cookie, _ = await create_user_and_session(
        db_session,
        status=UserStatus.DISABLED,
    )
    client.cookies.set("staff_session", disabled_session_cookie)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_revoked_session_is_rejected(client, db_session):
    _, revoked_session_cookie, _ = await create_user_and_session(
        db_session,
        revoked=True,
    )
    client.cookies.set("staff_session", revoked_session_cookie)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_rejected(client, db_session):
    _, expired_session_cookie, _ = await create_user_and_session(
        db_session,
        expired=True,
    )
    client.cookies.set("staff_session", expired_session_cookie)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_rejects_missing_csrf_header(client, db_session):
    _, session_cookie, _ = await create_user_and_session(db_session)
    client.cookies.set("staff_session", session_cookie)

    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_logout_rejects_incorrect_csrf_header(client, db_session):
    _, session_cookie, _ = await create_user_and_session(db_session)
    client.cookies.set("staff_session", session_cookie)

    response = await client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": "incorrect-token"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_logout_with_matching_csrf_revokes_session(client, db_session):
    _, session_cookie, csrf_token = await create_user_and_session(db_session)
    client.cookies.set("staff_session", session_cookie)

    response = await client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 204
    assert response.headers["set-cookie"].startswith('staff_session="')

    client.cookies.set("staff_session", session_cookie)
    rejected = await client.get("/api/v1/auth/me")
    assert rejected.status_code == 401


@pytest.mark.asyncio
async def test_callback_sets_secure_opaque_session_for_invited_staff(
    app,
    client,
    db_session,
):
    organization = Organization(name="Invited Staff")
    db_session.add(organization)
    await db_session.flush()
    invited = StaffUser(
        organization_id=organization.id,
        oidc_subject=None,
        email="invited@example.com",
        role=UserRole.REVIEWER,
        status=UserStatus.INVITED,
    )
    db_session.add(invited)
    await db_session.flush()

    class FakeOIDCClient:
        async def identity_from_callback(self, _request):
            return OIDCIdentity(
                subject="stable-google-subject",
                email="invited@example.com",
                email_verified=True,
            )

    app.dependency_overrides[get_oidc_client] = lambda: FakeOIDCClient()

    response = await client.get("/api/v1/auth/callback")

    assert response.status_code == 303
    session_cookie = response.cookies["staff_session"]
    assert "invited@example.com" not in session_cookie
    set_cookie = response.headers.get_list("set-cookie")
    staff_cookie = next(value for value in set_cookie if value.startswith("staff_session="))
    assert "HttpOnly" in staff_cookie
    assert "Secure" in staff_cookie
    assert "SameSite=lax" in staff_cookie

    client.cookies.set("staff_session", session_cookie)
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
