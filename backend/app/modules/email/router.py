import json
from collections.abc import Awaitable, Callable
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.delivery import (
    EmailDeliveryAuthorizationError,
    EmailDeliveryConflict,
    EmailDeliveryResult,
    EmailDeliveryService,
    ReconciliationRequired,
)
from app.modules.email.drafting import EmailDraftingService
from app.modules.email.models import EmailState
from app.modules.email.reads import EmailReadAuthorizationError, EmailReadService
from app.modules.email.reconciliation import ReconciliationService
from app.modules.email.review import (
    EmailReviewAuthorizationError,
    EmailReviewConflict,
    EmailReviewResult,
    EmailReviewService,
)
from app.modules.email.schemas import StaffEmailDetail, StaffEmailQueueItem
from app.modules.idempotency.models import IdempotencyState
from app.modules.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyLeaseLost,
    IdempotencyService,
)
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)

router = APIRouter(prefix="/api/v1/staff/email", tags=["email-review"])


@router.get("", response_model=list[StaffEmailQueueItem])
async def list_email_queue(
    states: list[EmailState] | None = Query(default=None, alias="state"),
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    try:
        return await EmailReadService(db_session, principal).queue(states)
    except EmailReadAuthorizationError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None


@router.get("/{work_item_id}", response_model=StaffEmailDetail)
async def read_email_detail(
    work_item_id: UUID,
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        return await EmailReadService(db_session, principal).detail(work_item_id)
    except EmailReadAuthorizationError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


class ReviewActionPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_version: int = Field(ge=1)
    current_draft_id: UUID


class RegeneratePayload(ReviewActionPayload):
    instruction: str = Field(min_length=1, max_length=4000)


class DraftPatch(ReviewActionPayload):
    body: str | None = Field(default=None, min_length=1)
    to: list[str] | None = None
    cc: list[str] | None = None
    subject: str | None = Field(default=None, min_length=1)
    thread_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def require_edit(self) -> "DraftPatch":
        if all(
            value is None
            for value in (self.body, self.to, self.cc, self.subject, self.thread_id)
        ):
            raise ValueError("at least one draft field is required")
        return self


class DeliveryActionPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_version: int = Field(ge=1)


class ReconciliationPayload(DeliveryActionPayload):
    confirm_absent: bool = False


def _write_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is required"
        )
    return value.strip()


def _response(result: EmailReviewResult) -> dict[str, object]:
    return {
        "id": str(result.id),
        "state": result.state.value,
        "version": result.version,
        "current_draft_id": str(result.current_draft_id),
    }


async def _perform(
    *,
    operation: str,
    work_item_id: UUID,
    request_payload: BaseModel,
    idempotency_key: str,
    principal: Principal,
    db_session: AsyncSession,
    invoke: Callable[[], Awaitable[EmailReviewResult]],
) -> dict[str, object]:
    service = IdempotencyService(db_session)
    try:
        record = await service.begin(
            scope_id=principal.organization_id,
            actor_id=principal.subject_id,
            operation=operation,
            object_id=work_item_id,
            key=idempotency_key,
            request_hash=sha256(
                json.dumps(
                    request_payload.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        if record.state is IdempotencyState.COMPLETED:
            if not isinstance(record.response_body, dict):
                raise IdempotencyConflict(idempotency_key)
            return record.response_body
        result = await invoke()
        response = _response(result)
        await service.complete(
            record.id,
            status.HTTP_200_OK,
            response,
            lease_token=record.lease_token,
            safe_response_keys=set(response),
        )
        await db_session.commit()
        return response
    except EmailReviewConflict as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "state": error.state.value,
                "version": error.version,
                "current_draft_id": (
                    str(error.current_draft_id) if error.current_draft_id is not None else None
                ),
            },
        ) from None
    except (IdempotencyConflict, IdempotencyInProgress, IdempotencyLeaseLost):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from None
    except EmailReviewAuthorizationError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
    except LookupError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from None


def _delivery_response(result: EmailDeliveryResult) -> dict[str, object]:
    return {
        "id": str(result.id),
        "work_item_id": str(result.work_item_id),
        "state": result.state.value,
        "version": result.version,
    }


async def _perform_delivery(
    *,
    operation: str,
    delivery_intent_id: UUID,
    request_payload: DeliveryActionPayload,
    idempotency_key: str,
    principal: Principal,
    db_session: AsyncSession,
    invoke: Callable[[], Awaitable[EmailDeliveryResult]],
) -> dict[str, object]:
    service = IdempotencyService(db_session)
    try:
        record = await service.begin(
            scope_id=principal.organization_id,
            actor_id=principal.subject_id,
            operation=operation,
            object_id=delivery_intent_id,
            key=idempotency_key,
            request_hash=sha256(
                json.dumps(
                    request_payload.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        if record.state is IdempotencyState.COMPLETED:
            if not isinstance(record.response_body, dict):
                raise IdempotencyConflict(idempotency_key)
            return record.response_body
        result = await invoke()
        if result.version != request_payload.expected_version + 1:
            raise IdempotencyConflict(idempotency_key)
        response = _delivery_response(result)
        await service.complete(
            record.id,
            status.HTTP_200_OK,
            response,
            lease_token=record.lease_token,
            safe_response_keys=set(response),
        )
        await db_session.commit()
        return response
    except (IdempotencyConflict, IdempotencyInProgress, IdempotencyLeaseLost):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from None
    except EmailDeliveryAuthorizationError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
    except EmailDeliveryConflict as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"state": error.state.value, "version": error.version},
        ) from None
    except LookupError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ReconciliationRequired:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="delivery reconciliation is required before retry",
        ) from None
    except ValueError as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from None


@router.post("/{work_item_id}/regenerate")
async def regenerate(
    work_item_id: UUID,
    payload: RegeneratePayload,
    request: Request,
    idempotency_key: str = Depends(_write_key),
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    grounded = getattr(request.app.state, "grounded_answer_service", None)
    if grounded is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Grounded drafting is not configured",
        )
    review = EmailReviewService(
        db_session,
        principal,
        drafting_service=EmailDraftingService(db_session, grounded, principal),
    )
    return await _perform(
        operation="email.draft.regenerate",
        work_item_id=work_item_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db_session=db_session,
        invoke=lambda: review.regenerate(
            work_item_id,
            instruction=payload.instruction,
            expected_version=payload.expected_version,
            current_draft_id=payload.current_draft_id,
        ),
    )


@router.patch("/{work_item_id}/draft")
async def edit_draft(
    work_item_id: UUID,
    payload: DraftPatch,
    idempotency_key: str = Depends(_write_key),
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    review = EmailReviewService(db_session, principal)
    return await _perform(
        operation="email.draft.edit",
        work_item_id=work_item_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db_session=db_session,
        invoke=lambda: review.edit(
            work_item_id,
            expected_version=payload.expected_version,
            current_draft_id=payload.current_draft_id,
            body=payload.body,
            to=payload.to,
            cc=payload.cc,
            subject=payload.subject,
            thread_id=payload.thread_id,
        ),
    )


@router.post("/{work_item_id}/approve")
async def approve(
    work_item_id: UUID,
    payload: ReviewActionPayload,
    request: Request,
    idempotency_key: str = Depends(_write_key),
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    settings = request.app.state.settings
    review = EmailReviewService(
        db_session, principal, message_id_domain=settings.gmail_message_id_domain
    )
    return await _perform(
        operation="email.review.approve",
        work_item_id=work_item_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db_session=db_session,
        invoke=lambda: review.approve(
            work_item_id,
            expected_version=payload.expected_version,
            current_draft_id=payload.current_draft_id,
        ),
    )


@router.post("/delivery/{delivery_intent_id}/retry")
async def retry_delivery(
    delivery_intent_id: UUID,
    payload: DeliveryActionPayload,
    idempotency_key: str = Depends(_write_key),
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    delivery = EmailDeliveryService(db_session, worker_id="manual-delivery-retry")
    return await _perform_delivery(
        operation="email.delivery.retry",
        delivery_intent_id=delivery_intent_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db_session=db_session,
        invoke=lambda: delivery.request_retry(
            delivery_intent_id, principal, expected_version=payload.expected_version
        ),
    )


@router.post("/delivery/{delivery_intent_id}/reconcile")
async def reconcile_delivery(
    delivery_intent_id: UUID,
    payload: ReconciliationPayload,
    request: Request,
    idempotency_key: str = Depends(_write_key),
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    connector_service = request.app.state.connector_service
    gateway_factory = request.app.state.gmail_gateway_factory
    if connector_service is None or gateway_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail delivery is not configured",
        )
    reconciliation = ReconciliationService(
        db_session,
        None,
        principal,
        connector_service=connector_service,
        gateway_factory=gateway_factory,
    )
    return await _perform_delivery(
        operation="email.delivery.reconcile",
        delivery_intent_id=delivery_intent_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db_session=db_session,
        invoke=lambda: reconciliation.reconcile(
            delivery_intent_id,
            confirm_absent=payload.confirm_absent,
            expected_version=payload.expected_version,
        ),
    )


@router.post("/{work_item_id}/reject")
async def reject(
    work_item_id: UUID,
    payload: ReviewActionPayload,
    idempotency_key: str = Depends(_write_key),
    principal: Principal = Depends(require_staff_csrf),
    db_session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    review = EmailReviewService(db_session, principal)
    return await _perform(
        operation="email.review.reject",
        work_item_id=work_item_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db_session=db_session,
        invoke=lambda: review.reject(
            work_item_id,
            expected_version=payload.expected_version,
            current_draft_id=payload.current_draft_id,
        ),
    )
