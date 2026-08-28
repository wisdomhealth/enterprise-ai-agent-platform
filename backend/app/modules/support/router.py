from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import ChatMessage, ChatSession, ConversationState
from app.modules.chat.router import _begin_idempotency, _credential_value, _request_hash
from app.modules.chat.service import ChatSessionService
from app.modules.idempotency.models import IdempotencyState
from app.modules.idempotency.service import IdempotencyService
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.support.models import Handoff, HandoffTrigger
from app.modules.support.service import SupportAuthorizationError, SupportService, VersionConflict

public_router = APIRouter(prefix="/api/v1/public/chat", tags=["public-chat"])
staff_router = APIRouter(prefix="/api/v1/staff/support", tags=["support"])


class PublicHandoffCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=320)


class StaffReply(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    body: str = Field(min_length=1, max_length=4000)
    version: int = Field(ge=1)


class StaffAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: int = Field(ge=1)


def _handoff_read(handoff: Handoff) -> dict[str, object]:
    return {
        "id": str(handoff.id),
        "session_id": str(handoff.session_id),
        "state": handoff.state.value,
        "trigger": handoff.trigger.value,
        "assigned_user_id": str(handoff.assigned_user_id) if handoff.assigned_user_id else None,
        "version": handoff.version,
        "last_customer_sequence": handoff.last_customer_sequence,
    }


def _conversation_read(
    handoff: Handoff, session: ChatSession, messages: list[ChatMessage]
) -> dict[str, object]:
    """Project only staff-authorized durable context; never a transient worker view."""

    payload = _handoff_read(handoff)
    snapshot = handoff.snapshot
    customer = snapshot.get("customer")
    payload.update(
        {
            "customer": customer
            if isinstance(customer, dict)
            else {"name": session.customer_name, "email": session.customer_email},
            "summary": snapshot.get("summary", ""),
            "tool_results": snapshot.get("tool_results", []),
            "messages": [
                {
                    "sequence": message.sequence,
                    "actor": message.actor.value,
                    "body": message.body,
                    "status": message.status.value,
                    "created_at": message.created_at.isoformat(),
                    # Task 10 customer-answer events deliberately project only
                    # customer citations. Rich internal sources are supplied by
                    # the separately authorized Staff Assist query.
                    "citations": [],
                }
                for message in messages
            ],
        }
    )
    return payload


async def _public_session(
    session_id: UUID,
    credential: str = Depends(_credential_value),
    db_session: AsyncSession = Depends(get_db_session),
) -> ChatSession:
    session = await ChatSessionService(db_session).get_authorized_session(
        session_id=session_id, credential_value=credential
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return session


def _write_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is required"
        )
    return value


@public_router.post("/sessions/{session_id}/handoff", status_code=status.HTTP_202_ACCEPTED)
async def request_public_handoff(
    session_id: UUID,
    payload: PublicHandoffCreate,
    _idempotency_key: str = Depends(_write_key),
    session: ChatSession = Depends(_public_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    del session_id
    if session.state is ConversationState.RESOLVED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    record = await _begin_idempotency(
        db_session,
        scope_id=session.organization_id,
        actor_id=uuid5(NAMESPACE_URL, f"public-chat-session:{session.id}"),
        operation="public_chat.handoff.request",
        object_id=session.id,
        key=_idempotency_key,
        request_hash=_request_hash(payload.model_dump(mode="json")),
    )
    if record.state is IdempotencyState.COMPLETED:
        if not isinstance(record.response_body, dict):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        return record.response_body
    if payload.contact_name is not None:
        session.customer_name = payload.contact_name
    if payload.contact_email is not None:
        session.customer_email = payload.contact_email
    try:
        handoff = await SupportService(db_session).request_handoff(
            session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST
        )
        response = _handoff_read(handoff)
        await IdempotencyService(db_session).complete(
            record.id,
            status.HTTP_202_ACCEPTED,
            response,
            lease_token=record.lease_token,
            safe_response_keys=set(response),
        )
        await db_session.commit()
    except (LookupError, ValueError):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from None
    return response


@staff_router.get("/queue")
async def list_queue(
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    try:
        return [_handoff_read(item) for item in await SupportService(db_session).queue(principal)]
    except SupportAuthorizationError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None


@staff_router.get("/{handoff_id}")
async def read_conversation(
    handoff_id: UUID,
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        handoff, session, messages = await SupportService(db_session).conversation(
            handoff_id, principal
        )
        return _conversation_read(handoff, session, messages)
    except SupportAuthorizationError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


async def _perform(
    operation: str,
    handoff_id: UUID,
    payload: StaffAction,
    principal: Principal,
    db_session: AsyncSession,
    body: str | None = None,
) -> dict[str, object]:
    service = SupportService(db_session)
    try:
        if operation == "claim":
            handoff = await service.claim(handoff_id, principal, payload.version)
        elif operation == "resolve":
            handoff = await service.resolve(handoff_id, principal, payload.version)
        elif operation == "resume":
            handoff = await service.resume_ai(handoff_id, principal, payload.version)
        else:
            assert body is not None
            message = await service.reply(handoff_id, principal, payload.version, body)
            await db_session.commit()
            return {
                "sequence": message.sequence,
                "actor": message.actor.value,
                "body": message.body,
            }
        await db_session.commit()
        return _handoff_read(handoff)
    except VersionConflict as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"state": error.state.value, "version": error.version},
        ) from None
    except SupportAuthorizationError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
    except LookupError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


@staff_router.post("/{handoff_id}/claim")
async def claim(
    handoff_id: UUID,
    payload: StaffAction,
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await _perform("claim", handoff_id, payload, principal, db_session)


@staff_router.post("/{handoff_id}/reply")
async def reply(
    handoff_id: UUID,
    payload: StaffReply,
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await _perform(
        "reply",
        handoff_id,
        StaffAction(version=payload.version),
        principal,
        db_session,
        payload.body,
    )


@staff_router.post("/{handoff_id}/resolve")
async def resolve(
    handoff_id: UUID,
    payload: StaffAction,
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await _perform("resolve", handoff_id, payload, principal, db_session)


@staff_router.post("/{handoff_id}/resume-ai")
async def resume_ai(
    handoff_id: UUID,
    payload: StaffAction,
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await _perform("resume", handoff_id, payload, principal, db_session)
