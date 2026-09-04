from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request

from app.modules.identity.models import StaffUser, UserRole, UserStatus
from app.modules.identity.oidc import GoogleOIDCClient, OIDCIdentity, configure_google_oidc
from app.modules.identity.service import AdmissionDenied, IdentityService


class StubSession:
    def __init__(self, staff_user: StaffUser | None) -> None:
        self.staff_user = staff_user
        self.flush_count = 0

    async def scalars(self, _statement: object) -> object:
        staff_users = [] if self.staff_user is None else [self.staff_user]

        class Result:
            def all(self) -> list[StaffUser]:
                return staff_users

        return Result()

    async def flush(self) -> None:
        self.flush_count += 1


def invited_user(*, email: str = "agent@example.com") -> StaffUser:
    return StaffUser(
        id=uuid4(),
        organization_id=uuid4(),
        oidc_subject=None,
        email=email,
        role=UserRole.REVIEWER,
        status=UserStatus.INVITED,
    )


@pytest.mark.asyncio
async def test_oidc_login_rejects_verified_but_uninvited_identity():
    identity_service = IdentityService(StubSession(None))
    identity = OIDCIdentity(
        subject="google-subject-7",
        email="outsider@example.com",
        email_verified=True,
    )

    with pytest.raises(AdmissionDenied):
        await identity_service.admit(identity)


@pytest.mark.asyncio
async def test_oidc_login_rejects_unverified_email():
    identity_service = IdentityService(StubSession(invited_user()))
    identity = OIDCIdentity(
        subject="google-subject-7",
        email="agent@example.com",
        email_verified=False,
    )

    with pytest.raises(AdmissionDenied):
        await identity_service.admit(identity)


@pytest.mark.asyncio
async def test_oidc_login_requires_an_exact_email_match():
    identity_service = IdentityService(StubSession(None))
    identity = OIDCIdentity(
        subject="google-subject-7",
        email="Agent@example.com",
        email_verified=True,
    )

    with pytest.raises(AdmissionDenied):
        await identity_service.admit(identity)


@pytest.mark.asyncio
async def test_oidc_login_rejects_disabled_invited_user():
    staff_user = invited_user()
    staff_user.status = UserStatus.DISABLED
    identity_service = IdentityService(StubSession(staff_user))

    with pytest.raises(AdmissionDenied):
        await identity_service.admit(
            OIDCIdentity(
                subject="google-subject-7",
                email=staff_user.email,
                email_verified=True,
            )
        )


@pytest.mark.asyncio
async def test_oidc_login_binds_stable_subject_and_activates_invited_user():
    staff_user = invited_user()
    session = StubSession(staff_user)
    identity_service = IdentityService(session)

    admitted = await identity_service.admit(
        OIDCIdentity(
            subject="google-subject-7",
            email=staff_user.email,
            email_verified=True,
        )
    )

    assert admitted is staff_user
    assert admitted.oidc_subject == "google-subject-7"
    assert admitted.status is UserStatus.ACTIVE
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_oidc_login_rejects_subject_change_for_bound_user():
    staff_user = invited_user()
    staff_user.status = UserStatus.ACTIVE
    staff_user.oidc_subject = "google-subject-original"
    identity_service = IdentityService(StubSession(staff_user))

    with pytest.raises(AdmissionDenied):
        await identity_service.admit(
            OIDCIdentity(
                subject="google-subject-attacker",
                email=staff_user.email,
                email_verified=True,
            )
        )


class CapturingOAuthRegistry:
    def __init__(self) -> None:
        self.registration: dict[str, object] = {}

    def register(self, **kwargs: object) -> object:
        self.registration = kwargs
        return object()


def test_google_oidc_registration_uses_only_login_scopes_and_s256_pkce():
    oauth = CapturingOAuthRegistry()

    configure_google_oidc(oauth, client_id="client-id", client_secret="client-secret")

    assert oauth.registration["server_metadata_url"] == (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    assert oauth.registration["client_kwargs"] == {
        "scope": "openid email profile",
        "code_challenge_method": "S256",
    }


@pytest.mark.asyncio
async def test_authlib_authorization_redirect_contains_pkce_challenge_and_nonce():
    oauth = OAuth()
    client = oauth.register(
        name="google",
        client_id="client-id",
        client_secret="client-secret",
        authorize_url="https://accounts.google.test/o/oauth2/v2/auth",
        access_token_url="https://oauth2.google.test/token",
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
        },
    )
    oidc = GoogleOIDCClient(client)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/login",
            "headers": [],
            "query_string": b"",
            "session": {},
        }
    )

    response = await oidc.authorization_redirect(
        request,
        "https://platform.example.com/api/v1/auth/callback",
    )

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert query["nonce"][0]
    state_record = next(iter(request.session.values()))
    assert state_record["data"]["nonce"] == query["nonce"][0]
    assert "code_verifier" in state_record["data"]
