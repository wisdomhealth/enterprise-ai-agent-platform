from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.email.models import (
    DeliveryAttempt,
    DeliveryIntent,
    EmailApproval,
    EmailDraftVersion,
    EmailState,
    EmailStateHistory,
    EmailWorkItem,
)
from app.modules.email.schemas import EmailCitation
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole


class EmailReadAuthorizationError(PermissionError):
    pass


class EmailReadService:
    """Authorized projections of durable email review and delivery state."""

    def __init__(
        self,
        db_session: AsyncSession,
        principal: Principal,
        *,
        authorization_service: AuthorizationService | None = None,
    ) -> None:
        self._db_session = db_session
        self._principal = principal
        self._authorization = authorization_service or AuthorizationService(db_session)

    async def queue(self, states: Iterable[EmailState] | None = None) -> list[dict[str, object]]:
        self._require_reviewer()
        query = select(EmailWorkItem).where(
            EmailWorkItem.organization_id == self._principal.organization_id
        )
        selected_states = tuple(states or ())
        if selected_states:
            query = query.where(EmailWorkItem.state.in_(selected_states))
        candidates = list(
            (await self._db_session.scalars(query.order_by(EmailWorkItem.received_at.desc()))).all()
        )
        visible: list[dict[str, object]] = []
        for item in candidates:
            try:
                await self._require_item(item)
            except EmailReadAuthorizationError:
                continue
            visible.append(_queue_projection(item))
        return visible

    async def detail(self, work_item_id: UUID) -> dict[str, object]:
        self._require_reviewer()
        item = await self._db_session.scalar(
            select(EmailWorkItem).where(
                EmailWorkItem.id == work_item_id,
                EmailWorkItem.organization_id == self._principal.organization_id,
            )
        )
        if item is None:
            raise LookupError("email work item not found")
        await self._require_item(item)

        drafts = list(
            (
                await self._db_session.scalars(
                    select(EmailDraftVersion)
                    .where(
                        EmailDraftVersion.work_item_id == item.id,
                        EmailDraftVersion.organization_id == item.organization_id,
                    )
                    .order_by(EmailDraftVersion.version)
                )
            ).all()
        )
        approvals = list(
            (
                await self._db_session.scalars(
                    select(EmailApproval).where(
                        EmailApproval.work_item_id == item.id,
                        EmailApproval.organization_id == item.organization_id,
                    )
                )
            ).all()
        )
        approval_by_draft = {approval.draft_version_id: approval for approval in approvals}
        history = list(
            (
                await self._db_session.scalars(
                    select(EmailStateHistory)
                    .where(
                        EmailStateHistory.work_item_id == item.id,
                        EmailStateHistory.organization_id == item.organization_id,
                    )
                    .order_by(EmailStateHistory.resource_version, EmailStateHistory.created_at)
                )
            ).all()
        )
        intent = await self._db_session.scalar(
            select(DeliveryIntent)
            .where(
                DeliveryIntent.work_item_id == item.id,
                DeliveryIntent.organization_id == item.organization_id,
                DeliveryIntent.approved_draft_version_id == item.current_draft_id,
            )
            .order_by(DeliveryIntent.created_at.desc())
            .limit(1)
        )
        attempts = (
            []
            if intent is None
            else list(
                (
                    await self._db_session.scalars(
                        select(DeliveryAttempt)
                        .where(DeliveryAttempt.delivery_intent_id == intent.id)
                        .order_by(DeliveryAttempt.attempt_number)
                    )
                ).all()
            )
        )
        return {
            **_queue_projection(item),
            "recipients": list(item.recipients),
            "body": item.body,
            "reply_required": item.reply_required,
            "classification_rationale": _classification_rationale(item),
            "current_draft_id": str(item.current_draft_id) if item.current_draft_id else None,
            "drafts": [
                _draft_projection(item, draft, approval_by_draft.get(draft.id))
                for draft in drafts
            ],
            "audit_transitions": [_history_projection(entry) for entry in history],
            "delivery": _delivery_projection(intent, attempts),
        }

    def _require_reviewer(self) -> None:
        if self._principal.role not in {UserRole.ADMIN, UserRole.REVIEWER}:
            raise EmailReadAuthorizationError

    async def _require_item(self, item: EmailWorkItem) -> None:
        try:
            await self._authorization.require(
                self._principal,
                "knowledge.review",
                ResourceRef(
                    organization_id=item.organization_id,
                    resource_type="knowledge",
                    resource_id=item.knowledge_base_id,
                    state=ResourceState.ACTIVE,
                ),
            )
        except AuthorizationDenied:
            raise EmailReadAuthorizationError from None


def _queue_projection(item: EmailWorkItem) -> dict[str, object]:
    return {
        "id": str(item.id),
        "state": item.state.value,
        "version": item.version,
        "sender": item.sender,
        "subject": item.subject,
        "received_at": item.received_at.isoformat(),
        "category": item.category.value if item.category else None,
        "priority": item.priority.value if item.priority else None,
    }


def _classification_rationale(item: EmailWorkItem) -> str:
    category = (
        "Unclassified"
        if item.category is None
        else item.category.value.replace("_", " ").title()
    )
    priority = "No priority" if item.priority is None else f"{item.priority.value.title()} priority"
    reply = (
        "Reply status unknown"
        if item.reply_required is None
        else "Reply required"
        if item.reply_required
        else "No reply required"
    )
    return " · ".join((category, priority, reply))


def _draft_projection(
    item: EmailWorkItem, draft: EmailDraftVersion, approval: EmailApproval | None
) -> dict[str, object]:
    return {
        "id": str(draft.id),
        "version": draft.version,
        "body": draft.body,
        "to": list(draft.to),
        "cc": list(draft.cc),
        "subject": draft.subject,
        "thread_id": draft.thread_id,
        "reviewer_instruction": draft.reviewer_instruction,
        "model": draft.model,
        "prompt_version": draft.prompt_version,
        "created_at": draft.created_at.isoformat(),
        "citations": [
            citation
            for value in draft.citations
            if (citation := _citation_projection(item, value)) is not None
        ],
        "approval": None
        if approval is None
        else {
            "approved_at": approval.approved_at.isoformat(),
            "invalidated_at": approval.invalidated_at.isoformat()
            if approval.invalidated_at
            else None,
        },
    }


def _citation_projection(item: EmailWorkItem, raw: Any) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    try:
        citation = EmailCitation.model_validate(raw)
    except ValidationError:
        return None
    if (
        citation.organization_id != item.organization_id
        or citation.knowledge_base_id != item.knowledge_base_id
    ):
        return None
    link = citation.internal_drive_link
    if link is not None:
        parsed = urlparse(link)
        if parsed.scheme != "https" or parsed.hostname != "drive.google.com":
            link = None
    return {
        "title": citation.title,
        "section": citation.section,
        "page_number": citation.page_number,
        "chunk_id": str(citation.chunk_id),
        "document_version_id": str(citation.document_version_id),
        "internal_drive_link": link,
    }


def _history_projection(entry: EmailStateHistory) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "from_state": entry.from_state.value,
        "to_state": entry.to_state.value,
        "action": entry.action.value,
        "reason_code": entry.reason_code,
        "actor_type": entry.actor_type,
        "created_at": entry.created_at.isoformat(),
    }


def _delivery_projection(
    intent: DeliveryIntent | None, attempts: list[DeliveryAttempt]
) -> dict[str, object] | None:
    if intent is None:
        return None
    return {
        "id": str(intent.id),
        "state": intent.state.value,
        "version": intent.version,
        "deterministic_message_id": intent.deterministic_message_id,
        "last_error_code": intent.last_error_code,
        "attempts": [
            {
                "id": str(attempt.id),
                "attempt_number": attempt.attempt_number,
                "outcome": attempt.outcome,
                "error_code": attempt.error_code,
                "started_at": attempt.started_at.isoformat(),
                "completed_at": attempt.completed_at.isoformat()
                if attempt.completed_at
                else None,
            }
            for attempt in attempts
        ],
    }
