import json
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import ChatSession
from app.modules.chat.rate_limit import (
    RateLimitExceeded,
    RateLimitUnavailable,
    SlidingWindowRateLimiter,
)
from app.modules.chat.schemas import (
    PublicChatCredentialRead,
    PublicChatSessionCreate,
    PublicChatSessionCreated,
    PublicChatSessionRead,
)
from app.modules.chat.service import ChatSessionService
from app.modules.idempotency.models import IdempotencyRecord, IdempotencyState
from app.modules.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyService,
)
from app.modules.identity.dependencies import get_db_session

router = APIRouter(prefix="/api/v1/public/chat", tags=["public-chat"])


def _ip_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _credential_value(authorization: str | None = Header(default=None)) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return token


def _idempotency_key(
    value: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str | None:
    return value or None


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return session


def _session_read(session: ChatSession) -> PublicChatSessionRead:
    return PublicChatSessionRead(
        id=session.id,
        state=session.state,
        version=session.version,
        customer_name=session.customer_name,
        customer_email=session.customer_email,
        created_at=session.created_at,
        messages=[],
    )


def _raise_rate_limit(error: RateLimitExceeded | RateLimitUnavailable) -> None:
    retry_after = error.retry_after if isinstance(error, RateLimitExceeded) else 1
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Please wait a moment before trying again.",
        headers={"Retry-After": str(retry_after)},
    ) from error


def _request_hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


async def _begin_idempotency(
    db_session: AsyncSession,
    *,
    scope_id: UUID,
    actor_id: UUID,
    operation: str,
    object_id: UUID,
    key: str,
    request_hash: str,
) -> IdempotencyRecord:
    try:
        return await IdempotencyService(db_session).begin(
            scope_id=scope_id,
            actor_id=actor_id,
            operation=operation,
            object_id=object_id,
            key=key,
            request_hash=request_hash,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error


@router.post(
    "/sessions",
    response_model=PublicChatSessionCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_public_session(
    payload: PublicChatSessionCreate,
    request: Request,
    idempotency_key: str | None = Depends(_idempotency_key),
    db_session: AsyncSession = Depends(get_db_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatSessionCreated:
    ip_address = _ip_address(request)
    knowledge_base = await service.knowledge_base_for_public_key(payload.public_key)
    if knowledge_base is None:
        try:
            await service.check_invalid_creation_attempt(ip_address=ip_address)
        except (RateLimitExceeded, RateLimitUnavailable) as error:
            _raise_rate_limit(error)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    record = None
    if idempotency_key is not None:
        record = await _begin_idempotency(
            db_session,
            scope_id=knowledge_base.organization_id,
            actor_id=uuid5(NAMESPACE_URL, f"public-chat-ip:{ip_address}"),
            operation="public_chat.session.create",
            object_id=knowledge_base.id,
            key=idempotency_key,
            request_hash=_request_hash(payload.model_dump(mode="json")),
        )
        if record.state is IdempotencyState.COMPLETED:
            return PublicChatSessionCreated.model_validate(record.response_body)
    try:
        await service.check_creation_admission(
            ip_address=ip_address, organization_id=knowledge_base.organization_id
        )
        session, token, expires_at = await service.create_session_for_knowledge_base(
            knowledge_base=knowledge_base,
            customer_name=payload.customer_name,
            customer_email=payload.customer_email,
        )
    except (RateLimitExceeded, RateLimitUnavailable) as error:
        _raise_rate_limit(error)
    response = PublicChatSessionCreated(
        session=_session_read(session),
        credential=PublicChatCredentialRead(token=token, expires_at=expires_at),
    )
    if record is not None:
        await IdempotencyService(db_session).complete(
            record.id,
            status.HTTP_201_CREATED,
            response.model_dump(mode="json"),
            lease_token=record.lease_token,
            safe_response_keys=("session", "credential"),
        )
    await db_session.commit()
    return response


@router.get("/sessions/{session_id}", response_model=PublicChatSessionRead)
async def read_public_session(
    session: ChatSession = Depends(_authorized_session),
) -> PublicChatSessionRead:
    return _session_read(session)


@router.post("/sessions/{session_id}/credentials/rotate", response_model=PublicChatCredentialRead)
async def rotate_public_credential(
    session_id: UUID,
    request: Request,
    credential: str = Depends(_credential_value),
    idempotency_key: str | None = Depends(_idempotency_key),
    db_session: AsyncSession = Depends(get_db_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatCredentialRead:
    session = await db_session.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    record = None
    if idempotency_key is not None:
        record = await _begin_idempotency(
            db_session,
            scope_id=session.organization_id,
            actor_id=uuid5(NAMESPACE_URL, f"public-chat-token:{credential}"),
            operation="public_chat.credential.rotate",
            object_id=session.id,
            key=idempotency_key,
            request_hash=_request_hash({"session_id": str(session_id)}),
        )
        if record.state is IdempotencyState.COMPLETED:
            return PublicChatCredentialRead.model_validate(record.response_body)
    try:
        await service.check_rotation_admission(
            ip_address=_ip_address(request),
            session_id=session.id,
            organization_id=session.organization_id,
        )
    except (RateLimitExceeded, RateLimitUnavailable) as error:
        _raise_rate_limit(error)
    try:
        rotated = await service.rotate_credential(
            session_id=session_id, credential_value=credential
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error
    if rotated is None:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    token, expires_at = rotated
    response = PublicChatCredentialRead(token=token, expires_at=expires_at)
    if record is not None:
        await IdempotencyService(db_session).complete(
            record.id,
            status.HTTP_200_OK,
            response.model_dump(mode="json"),
            lease_token=record.lease_token,
            safe_response_keys=("token", "expires_at"),
        )
    await db_session.commit()
    return response
