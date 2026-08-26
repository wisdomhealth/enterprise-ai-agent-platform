import json
from hashlib import sha256
from typing import cast
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
from app.modules.chat.tokens import derive_idempotent_chat_token
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
) -> str:
    if value is None or not value.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return value


def _chat_credential_secret(request: Request) -> str:
    session_secret = request.app.state.settings.session_secret
    if session_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Public chat is unavailable.",
        )
    return cast(str, session_secret.get_secret_value())


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


def _credential_replay(
    *, record: IdempotencyRecord, session_secret: str, expected_session_id: UUID | None = None
) -> PublicChatCredentialRead:
    try:
        body = record.response_body or {}
        reference = body["credential"]
        if not isinstance(reference, dict):
            raise ValueError("credential reference is missing")
        session_id = UUID(str(reference["session_id"]))
        UUID(str(reference["credential_id"]))
        if expected_session_id is not None and session_id != expected_session_id:
            raise ValueError("credential session does not match idempotency binding")
        expires_at = PublicChatCredentialRead.model_validate(
            {"token": "placeholder", "expires_at": reference["expires_at"]}
        ).expires_at
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error
    return PublicChatCredentialRead(
        token=derive_idempotent_chat_token(
            session_secret=session_secret,
            idempotency_record_id=record.id,
            operation=record.operation,
            session_id=session_id,
        ),
        expires_at=expires_at,
    )


def _created_replay(record: IdempotencyRecord, session_secret: str) -> PublicChatSessionCreated:
    try:
        body = record.response_body or {}
        session = PublicChatSessionRead.model_validate(body["session"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error
    return PublicChatSessionCreated(
        session=session,
        credential=_credential_replay(
            record=record, session_secret=session_secret, expected_session_id=session.id
        ),
    )


def _credential_reference(
    *, session_id: UUID, credential_id: UUID, expires_at: object
) -> dict[str, str]:
    return {
        "session_id": str(session_id),
        "credential_id": str(credential_id),
        "expires_at": str(expires_at),
    }


@router.post(
    "/sessions",
    response_model=PublicChatSessionCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_public_session(
    payload: PublicChatSessionCreate,
    request: Request,
    idempotency_key: str = Depends(_idempotency_key),
    db_session: AsyncSession = Depends(get_db_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatSessionCreated:
    session_secret = _chat_credential_secret(request)
    ip_address = _ip_address(request)
    knowledge_base = await service.knowledge_base_for_public_key(payload.public_key)
    if knowledge_base is None:
        try:
            await service.check_invalid_creation_attempt(ip_address=ip_address)
        except (RateLimitExceeded, RateLimitUnavailable) as error:
            _raise_rate_limit(error)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

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
        return _created_replay(record, session_secret)
    try:
        await service.check_creation_admission(
            ip_address=ip_address, organization_id=knowledge_base.organization_id
        )
        session, credential, token, expires_at = await service.create_session_for_knowledge_base(
            knowledge_base=knowledge_base,
            customer_name=payload.customer_name,
            customer_email=payload.customer_email,
            credential_value_for_session=lambda session_id: derive_idempotent_chat_token(
                session_secret=session_secret,
                idempotency_record_id=record.id,
                operation=record.operation,
                session_id=session_id,
            ),
        )
    except (RateLimitExceeded, RateLimitUnavailable) as error:
        _raise_rate_limit(error)
    response = PublicChatSessionCreated(
        session=_session_read(session),
        credential=PublicChatCredentialRead(token=token, expires_at=expires_at),
    )
    await IdempotencyService(db_session).complete(
        record.id,
        status.HTTP_201_CREATED,
        {
            "session": response.session.model_dump(mode="json"),
            "credential": _credential_reference(
                session_id=session.id,
                credential_id=credential.id,
                expires_at=expires_at.isoformat(),
            ),
        },
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
    idempotency_key: str = Depends(_idempotency_key),
    db_session: AsyncSession = Depends(get_db_session),
    service: ChatSessionService = Depends(_service),
) -> PublicChatCredentialRead:
    session_secret = _chat_credential_secret(request)
    session = await db_session.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
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
        return _credential_replay(
            record=record, session_secret=session_secret, expected_session_id=session.id
        )
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
            session_id=session_id,
            credential_value=credential,
            replacement_credential_value=derive_idempotent_chat_token(
                session_secret=session_secret,
                idempotency_record_id=record.id,
                operation=record.operation,
                session_id=session.id,
            ),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from error
    if rotated is None:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    replacement, token, expires_at = rotated
    response = PublicChatCredentialRead(token=token, expires_at=expires_at)
    await IdempotencyService(db_session).complete(
        record.id,
        status.HTTP_200_OK,
        {
            "credential": _credential_reference(
                session_id=session.id,
                credential_id=replacement.id,
                expires_at=expires_at.isoformat(),
            )
        },
        lease_token=record.lease_token,
        safe_response_keys=("credential",),
    )
    await db_session.commit()
    return response
