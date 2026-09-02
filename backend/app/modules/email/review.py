from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.email.models import (
    EmailAction,
    EmailApproval,
    EmailDraftVersion,
    EmailState,
    EmailStateHistory,
    EmailWorkItem,
)
from app.modules.email.schemas import EmailDraftResult
from app.modules.email.state_machine import InvalidEmailTransition, transition
from app.modules.identity.dependencies import Principal, ServicePrincipal
from app.modules.identity.models import UserRole
from app.modules.outbox.service import OutboxService


class DraftRegenerator(Protocol):
    async def generate(
        self,
        work_item_id: UUID,
        *,
        job_id: UUID | None = None,
        reviewer_instruction: str | None = None,
    ) -> EmailDraftResult: ...


class EmailReviewAuthorizationError(PermissionError):
    pass


class EmailReviewConflict(RuntimeError):
    def __init__(self, item: EmailWorkItem) -> None:
        super().__init__(f"email is {item.state.value} at version {item.version}")
        self.state = item.state
        self.version = item.version
        self.current_draft_id = item.current_draft_id


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    id: UUID
    draft_version_id: UUID
    reviewer_id: UUID
    invalidated_at: object | None


@dataclass(frozen=True, slots=True)
class EmailReviewResult:
    id: UUID
    state: EmailState
    version: int
    current_draft_id: UUID
    approval: ApprovalResult | None


async def persist_generated_draft_version(
    db_session: AsyncSession,
    item: EmailWorkItem,
    principal: Principal,
    *,
    reviewer_instruction: str | None,
) -> EmailDraftVersion:
    """Publish one generated draft as a new immutable version in the current transaction."""

    if item.draft_body is None:
        raise RuntimeError("generated draft body is unavailable")
    previous = (
        await db_session.get(EmailDraftVersion, item.current_draft_id)
        if item.current_draft_id is not None
        else None
    )
    if previous is not None and previous.work_item_id != item.id:
        raise RuntimeError("current draft does not belong to email work item")
    provenance = dict(item.draft_provenance)
    draft = EmailDraftVersion(
        work_item_id=item.id,
        organization_id=item.organization_id,
        version=1 if previous is None else previous.version + 1,
        body=item.draft_body,
        to=[item.sender] if previous is None else list(previous.to),
        cc=[] if previous is None else list(previous.cc),
        subject=_reply_subject(item.subject) if previous is None else previous.subject,
        thread_id=item.gmail_thread_id if previous is None else previous.thread_id,
        reviewer_instruction=reviewer_instruction,
        model=str(provenance.pop("model", "unknown")),
        prompt_version=str(provenance.pop("prompt_version", "unknown")),
        retrieval_config=provenance,
        citations=list(item.draft_citations),
        created_by_id=principal.subject_id,
        creator_type="SYSTEM" if isinstance(principal, ServicePrincipal) else "STAFF",
    )
    db_session.add(draft)
    await db_session.flush()
    item.current_draft_id = draft.id
    await db_session.flush()
    return draft


class EmailReviewService:
    def __init__(
        self,
        db_session: AsyncSession,
        principal: Principal,
        *,
        drafting_service: DraftRegenerator | None = None,
        authorization_service: AuthorizationService | None = None,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._principal = principal
        self._drafting_service = drafting_service
        self._authorization = authorization_service or AuthorizationService(db_session)
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()

    async def regenerate(
        self,
        work_item_id: UUID,
        *,
        instruction: str,
        expected_version: int,
        current_draft_id: UUID,
    ) -> EmailReviewResult:
        if not instruction.strip():
            raise ValueError("reviewer instruction is required")
        if self._drafting_service is None:
            raise RuntimeError("email drafting service is unavailable")
        item, _ = await self._lock_current(
            work_item_id, expected_version=expected_version, current_draft_id=current_draft_id
        )
        if item.state is not EmailState.AWAITING_REVIEW:
            raise EmailReviewConflict(item)
        self._change_state(item, EmailAction.START_DRAFT, reason_code="REVIEWER_REGENERATION")
        await self._db_session.flush()
        await self._drafting_service.generate(
            item.id, reviewer_instruction=instruction.strip()
        )
        event_type = (
            "email.draft.regenerated"
            if item.state is EmailState.AWAITING_REVIEW
            else "email.draft.regeneration_failed"
        )
        await self._record(item, "email.draft.regenerate", event_type)
        return await self._result(item)

    async def edit(
        self,
        work_item_id: UUID,
        *,
        expected_version: int,
        current_draft_id: UUID,
        body: str | None = None,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        subject: str | None = None,
        thread_id: str | None = None,
    ) -> EmailReviewResult:
        item, current = await self._lock_current(
            work_item_id, expected_version=expected_version, current_draft_id=current_draft_id
        )
        if item.state not in {EmailState.AWAITING_REVIEW, EmailState.APPROVED}:
            raise EmailReviewConflict(item)
        next_body = current.body if body is None else body
        next_to = list(current.to) if to is None else to
        next_cc = list(current.cc) if cc is None else cc
        next_subject = current.subject if subject is None else subject
        next_thread_id = current.thread_id if thread_id is None else thread_id
        proposed: dict[str, object] = {
            "body": next_body,
            "to": next_to,
            "cc": next_cc,
            "subject": next_subject,
            "thread_id": next_thread_id,
        }
        changed_fields = [
            field
            for field, value in proposed.items()
            if value != getattr(current, field)
        ]
        if not changed_fields:
            raise ValueError("draft edit does not change any field")
        if not next_body.strip() or not next_subject.strip():
            raise ValueError("draft body and subject must not be empty")
        if not next_to or not all(isinstance(value, str) and value.strip() for value in next_to):
            raise ValueError("at least one recipient is required")
        if not all(isinstance(value, str) and value.strip() for value in next_cc):
            raise ValueError("cc recipients must not be empty")

        invalidated = await self._invalidate_approval(item)
        draft = EmailDraftVersion(
            work_item_id=item.id,
            organization_id=item.organization_id,
            version=current.version + 1,
            body=next_body,
            to=list(next_to),
            cc=list(next_cc),
            subject=next_subject,
            thread_id=next_thread_id,
            reviewer_instruction=None,
            model=current.model,
            prompt_version=current.prompt_version,
            retrieval_config=dict(current.retrieval_config),
            citations=list(current.citations),
            created_by_id=self._principal.subject_id,
            creator_type="STAFF",
        )
        self._db_session.add(draft)
        await self._db_session.flush()
        item.current_draft_id = draft.id
        item.draft_body = draft.body
        item.draft_citations = list(draft.citations)
        item.draft_provenance = {
            "model": draft.model,
            "prompt_version": draft.prompt_version,
            **draft.retrieval_config,
        }
        self._change_state(item, EmailAction.DRAFT_READY, reason_code="REVIEWER_EDIT")
        await self._record(
            item,
            "email.draft.edit",
            "email.draft.edited",
            changed_fields=changed_fields,
            approval_id=invalidated.id if invalidated is not None else None,
        )
        return await self._result(item, fallback_approval=invalidated)

    async def approve(
        self,
        work_item_id: UUID,
        *,
        expected_version: int,
        current_draft_id: UUID,
    ) -> EmailReviewResult:
        item, draft = await self._lock_current(
            work_item_id, expected_version=expected_version, current_draft_id=current_draft_id
        )
        if item.state is not EmailState.AWAITING_REVIEW:
            raise EmailReviewConflict(item)
        approval = EmailApproval(
            work_item_id=item.id,
            organization_id=item.organization_id,
            draft_version_id=draft.id,
            reviewer_id=self._principal.subject_id,
        )
        self._db_session.add(approval)
        self._change_state(item, EmailAction.APPROVE)
        await self._db_session.flush()
        await self._record(
            item,
            "email.review.approve",
            "email.review.approved",
            approval_id=approval.id,
        )
        return await self._result(item, fallback_approval=approval)

    async def reject(
        self,
        work_item_id: UUID,
        *,
        expected_version: int,
        current_draft_id: UUID,
    ) -> EmailReviewResult:
        item, _ = await self._lock_current(
            work_item_id, expected_version=expected_version, current_draft_id=current_draft_id
        )
        if item.state is not EmailState.AWAITING_REVIEW:
            raise EmailReviewConflict(item)
        self._change_state(item, EmailAction.REJECT)
        await self._record(item, "email.review.reject", "email.review.rejected")
        return await self._result(item)

    async def _lock_current(
        self, work_item_id: UUID, *, expected_version: int, current_draft_id: UUID
    ) -> tuple[EmailWorkItem, EmailDraftVersion]:
        item = await self._db_session.scalar(
            select(EmailWorkItem)
            .where(
                EmailWorkItem.id == work_item_id,
                EmailWorkItem.organization_id == self._principal.organization_id,
            )
            .with_for_update()
        )
        if item is None:
            raise LookupError("email work item not found")
        await self._require_review(item)
        if item.version != expected_version or item.current_draft_id != current_draft_id:
            raise EmailReviewConflict(item)
        draft = await self._db_session.get(EmailDraftVersion, item.current_draft_id)
        if (
            draft is None
            or draft.work_item_id != item.id
            or draft.organization_id != item.organization_id
        ):
            raise EmailReviewConflict(item)
        return item, draft

    async def _require_review(self, item: EmailWorkItem) -> None:
        if self._principal.role not in {UserRole.ADMIN, UserRole.REVIEWER}:
            raise EmailReviewAuthorizationError
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
            raise EmailReviewAuthorizationError from None

    def _change_state(
        self, item: EmailWorkItem, action: EmailAction, *, reason_code: str | None = None
    ) -> None:
        before = item.state
        try:
            item.state = transition(before, action)
        except InvalidEmailTransition:
            raise EmailReviewConflict(item) from None
        item.version += 1
        self._db_session.add(
            EmailStateHistory(
                work_item_id=item.id,
                organization_id=item.organization_id,
                from_state=before,
                to_state=item.state,
                action=action,
                reason_code=reason_code,
                actor_id=self._principal.subject_id,
                actor_type="STAFF",
                resource_version=item.version,
            )
        )

    async def _invalidate_approval(self, item: EmailWorkItem) -> EmailApproval | None:
        approval = await self._db_session.scalar(
            select(EmailApproval)
            .where(
                EmailApproval.work_item_id == item.id,
                EmailApproval.invalidated_at.is_(None),
            )
            .order_by(EmailApproval.approved_at.desc())
            .with_for_update()
        )
        if approval is not None:
            approval.invalidated_at = await self._db_session.scalar(
                select(func.current_timestamp())
            )
            await self._db_session.flush()
        return approval

    async def _record(
        self,
        item: EmailWorkItem,
        action: str,
        event_type: str,
        *,
        changed_fields: list[str] | None = None,
        approval_id: UUID | None = None,
    ) -> None:
        details: dict[str, object] = {
            "state": item.state.value,
            "version": item.version,
            "current_draft_id": str(item.current_draft_id),
        }
        if changed_fields is not None:
            details["changed_fields"] = changed_fields
        if approval_id is not None:
            details["approval_id"] = str(approval_id)
        safe_keys = set(details)
        await self._audit.record(
            self._db_session,
            self._principal,
            action=action,
            object_type="email_work_item",
            object_id=item.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=safe_keys,
        )
        await self._outbox.add(
            self._db_session,
            event_type,
            "email_work_item",
            item.id,
            {
                "organization_id": str(item.organization_id),
                **details,
            },
        )

    async def _result(
        self, item: EmailWorkItem, *, fallback_approval: EmailApproval | None = None
    ) -> EmailReviewResult:
        if item.current_draft_id is None:
            raise RuntimeError("email work item has no current draft")
        approval = await self._db_session.scalar(
            select(EmailApproval)
            .where(EmailApproval.work_item_id == item.id)
            .order_by(EmailApproval.approved_at.desc())
            .limit(1)
        )
        approval = approval or fallback_approval
        approval_result = (
            ApprovalResult(
                id=approval.id,
                draft_version_id=approval.draft_version_id,
                reviewer_id=approval.reviewer_id,
                invalidated_at=approval.invalidated_at,
            )
            if approval is not None
            else None
        )
        return EmailReviewResult(
            id=item.id,
            state=item.state,
            version=item.version,
            current_draft_id=item.current_draft_id,
            approval=approval_result,
        )


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"
