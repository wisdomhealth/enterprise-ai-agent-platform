from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.chat.rate_limit import RateLimitExceeded, SlidingWindowRateLimiter
from app.modules.chat.schemas import (
    PublicChatCredentialRead,
    PublicChatMessageCreate,
    PublicChatMessageRead,
    PublicChatSessionCreate,
    PublicChatSessionCreated,
    PublicChatSessionRead,
)
from app.modules.chat.service import ChatSessionService
from app.modules.identity.dependencies import get_db_session

router = APIRouter(prefix="/api/v1/public/chat", tags=["public-chat"])


def _ip_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _credential_value(authorization: str | None = Header(default=None)) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return token


def _service(
    request: Request, db_session: AsyncSession = Depends(get_db_session)
) -> ChatSessionService:
    limiter = getattr(request.app.state, "chat_rate_limiter", None)
    if limiter is not None and not isinstance(limiter, SlidingWindowRateLimiter):
        limiter = None
    return ChatSessionService(db_session, rate_limiter=limiter)


async def _authorized_session(
    session_id: UUID,
    credential: str = Depends(_credential_value),
    service: ChatSessionService = Depends(_service),
) -> ChatSession:
    session = await service.get_authorized_session(
        session_id=session_id, credential_value=credential
    )
    if session is None:
        # Hide both existence and credential validity from anonymous callers.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return session


def _message_read(message: ChatMessage) -> PublicChatMessageRead:
    return PublicChatMessageRead(
        sequence=message.sequence,
        actor=message.actor,
        body=message.body,
        status=message.status,
        created_at=message.created_at,
    )


async def _session_read(session: ChatSession, service: ChatSessionService) -> PublicChatSessionRead:
    return PublicChatSessionRead(
        id=session.id,
        state=session.state,
        version=session.version,
        customer_name=session.customer_name,
        customer_email=session.customer_email,
        created_at=session.created_at,
        messages=[
            _message_read(message)
            for message in await service.messages_for(session_id=session.id)
        ],
    )


def _raise_rate_limit(error: RateLimitExceeded) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Please wait a moment before trying again.",
        headers={"Retry-After": str(error.retry_after)},
    ) from error


@router.post(
    "/sessions",
    response_model=PublicChatSessionCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_public_session(
    payload: PublicChatSessionCreate,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatSessionCreated:
    try:
        created = await service.create_session(
            public_key=payload.public_key,
            customer_name=payload.customer_name,
            customer_email=payload.customer_email,
            ip_address=_ip_address(request),
        )
    except RateLimitExceeded as error:
        _raise_rate_limit(error)
    if created is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    session, token, expires_at = created
    await db_session.commit()
    return PublicChatSessionCreated(
        session=await _session_read(session, service),
        credential=PublicChatCredentialRead(token=token, expires_at=expires_at),
    )


@router.get("/sessions/{session_id}", response_model=PublicChatSessionRead)
async def read_public_session(
    session: ChatSession = Depends(_authorized_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatSessionRead:
    return await _session_read(session, service)


@router.post("/sessions/{session_id}/credentials/rotate", response_model=PublicChatCredentialRead)
async def rotate_public_credential(
    db_session: AsyncSession = Depends(get_db_session),
    session: ChatSession = Depends(_authorized_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatCredentialRead:
    rotated = await service.rotate_credential(session=session)
    if rotated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    token, expires_at = rotated
    await db_session.commit()
    return PublicChatCredentialRead(token=token, expires_at=expires_at)


@router.post("/sessions/{session_id}/messages", response_model=PublicChatMessageRead)
async def create_public_message(
    payload: PublicChatMessageCreate,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    session: ChatSession = Depends(_authorized_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatMessageRead:
    try:
        message = await service.add_customer_message(
            session=session, body=payload.body, ip_address=_ip_address(request)
        )
    except RateLimitExceeded as error:
        _raise_rate_limit(error)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error
    await db_session.commit()
    return _message_read(message)
