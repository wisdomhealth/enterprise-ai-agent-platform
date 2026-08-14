from dataclasses import dataclass
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import Response

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_LOGIN_SCOPES = "openid email profile"


@dataclass(frozen=True, slots=True)
class OIDCIdentity:
    subject: str
    email: str
    email_verified: bool


class OAuthClient(Protocol):
    async def authorize_redirect(
        self,
        request: Request,
        redirect_uri: str,
        **kwargs: object,
    ) -> Response: ...

    async def authorize_access_token(
        self,
        request: Request,
        **kwargs: object,
    ) -> dict[str, Any]: ...


class OAuthRegistry(Protocol):
    def register(self, **kwargs: object) -> OAuthClient: ...


class GoogleOIDCClient:
    def __init__(self, client: OAuthClient) -> None:
        self._client = client

    async def authorization_redirect(self, request: Request, redirect_uri: str) -> Response:
        return await self._client.authorize_redirect(request, redirect_uri)

    async def identity_from_callback(self, request: Request) -> OIDCIdentity:
        token = await self._client.authorize_access_token(request)
        userinfo = token.get("userinfo")
        if not isinstance(userinfo, dict):
            raise ValueError("Google OIDC response did not contain validated user information")
        subject = userinfo.get("sub")
        email = userinfo.get("email")
        email_verified = userinfo.get("email_verified")
        if not isinstance(subject, str) or not isinstance(email, str):
            raise ValueError("Google OIDC response contained invalid identity claims")
        return OIDCIdentity(
            subject=subject,
            email=email,
            email_verified=email_verified is True,
        )


def configure_google_oidc(
    oauth: OAuthRegistry,
    *,
    client_id: str,
    client_secret: str,
) -> GoogleOIDCClient:
    client = oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=GOOGLE_DISCOVERY_URL,
        client_kwargs={
            "scope": GOOGLE_LOGIN_SCOPES,
            "code_challenge_method": "S256",
        },
    )
    return GoogleOIDCClient(client)
