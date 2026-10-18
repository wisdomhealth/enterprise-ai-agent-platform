from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.push import (
    GmailPushAuthenticator,
    GmailPushMailboxNotFound,
    GmailPushService,
    GmailPushValidationError,
)
from app.modules.identity.dependencies import get_db_session

router = APIRouter(prefix="/api/v1/webhooks/gmail", tags=["gmail-push"])


async def _bounded_body(request: Request, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise GmailPushValidationError("GMAIL_PUSH_BODY_TOO_LARGE")
    return bytes(body)


@router.post("/pubsub", status_code=status.HTTP_204_NO_CONTENT)
async def receive_gmail_push(
    request: Request,
    authorization: str | None = Header(default=None),
    db_session: AsyncSession = Depends(get_db_session),
) -> Response:
    settings = request.app.state.settings
    authenticator: GmailPushAuthenticator | None = getattr(
        request.app.state, "gmail_push_authenticator", None
    )
    if not settings.gmail_push_enabled or authenticator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail push is not configured",
        )
    try:
        body = await _bounded_body(request, settings.gmail_push_max_body_bytes)
        notification = await authenticator.verify(authorization, body)
        await GmailPushService(
            db_session,
            receipt_retention_days=settings.gmail_push_receipt_retention_days,
        ).accept(notification)
    except GmailPushValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from None
    except GmailPushMailboxNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
