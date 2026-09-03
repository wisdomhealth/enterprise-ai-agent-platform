from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import Action, ResourceRef, ResourceState
from app.modules.chat.models import ChatMessage, ChatSession
from app.modules.email.models import EmailDraftVersion, EmailWorkItem
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.service import JobService
from app.modules.knowledge.models import Document
from app.modules.outbox.models import OutboxEvent
from app.modules.outbox.service import OutboxService
from app.modules.retention.models import (
    ErasureRequest,
    ErasureScope,
    ErasureStatus,
    ErasureTarget,
    ErasureTargetType,
    RetentionPolicy,
)
from app.modules.support.models import Handoff


def subject_key_hash(key: bytes, subject_ref: str) -> str:
    if not key:
        raise ValueError("erasure hash key is required")
    normalized = subject_ref.strip().casefold()
    if not normalized:
        raise ValueError("subject reference is required")
    return hmac_new(key, normalized.encode(), sha256).hexdigest()


class RetentionAuthorizationError(PermissionError):
    pass


class RetentionConflict(RuntimeError):
    def __init__(self, policy: RetentionPolicy) -> None:
        self.version = policy.version
        super().__init__(f"retention policy is at version {policy.version}")


@dataclass(frozen=True, slots=True)
class RetentionResult:
    chat_messages: int = 0
    email_items: int = 0
    email_drafts: int = 0
    audit_events: int = 0

    @property
    def total(self) -> int:
        return self.chat_messages + self.email_items + self.email_drafts + self.audit_events


class RetentionService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        authorization_service: AuthorizationService | None = None,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._authorization = authorization_service or AuthorizationService(db_session)
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()

    async def policy(self, principal: Principal) -> RetentionPolicy:
        self._require_admin(principal)
        policy = await self._policy_for_organization(principal.organization_id)
        await self._require_policy_action(principal, policy, "retention.read")
        return policy

    async def update_policy(
        self,
        principal: Principal,
        *,
        expected_version: int,
        chat_days: int,
        email_days: int,
        audit_days: int,
    ) -> RetentionPolicy:
        self._require_admin(principal)
        if min(chat_days, email_days, audit_days) <= 0:
            raise ValueError("retention periods must be positive")
        policy = await self._policy_for_organization(principal.organization_id)
        await self._require_policy_action(principal, policy, "retention.write")
        previous = _policy_values(policy)
        updated = await self._db_session.scalar(
            update(RetentionPolicy)
            .where(
                RetentionPolicy.id == policy.id,
                RetentionPolicy.organization_id == principal.organization_id,
                RetentionPolicy.version == expected_version,
            )
            .values(
                chat_days=chat_days,
                email_days=email_days,
                audit_days=audit_days,
                version=RetentionPolicy.version + 1,
                updated_at=func.clock_timestamp(),
            )
            .returning(RetentionPolicy)
        )
        if not isinstance(updated, RetentionPolicy):
            current = await self._policy_for_organization(principal.organization_id)
            raise RetentionConflict(current)
        current_values = _policy_values(updated)
        await self._audit.record(
            self._db_session,
            principal,
            action="retention.policy.update",
            object_type="retention_policy",
            object_id=updated.id,
            outcome="SUCCESS",
            details={"previous": previous, "current": current_values},
            safe_detail_keys={"previous", "current"},
        )
        await self._outbox.add(
            self._db_session,
            "retention.policy.updated",
            "retention_policy",
            updated.id,
            {
                "organization_id": str(updated.organization_id),
                "previous": previous,
                "current": current_values,
            },
        )
        return updated

    async def apply_due(
        self,
        organization_id: UUID,
        *,
        now: datetime,
        batch_size: int = 500,
    ) -> RetentionResult:
        if batch_size <= 0:
            raise ValueError("batch size must be positive")
        policy = await self._policy_for_organization(organization_id)
        chat_cutoff = now - timedelta(days=policy.chat_days)
        email_cutoff = now - timedelta(days=policy.email_days)
        audit_cutoff = now - timedelta(days=policy.audit_days)

        chat_ids = list(
            (
                await self._db_session.scalars(
                    select(ChatSession.id)
                    .where(
                        ChatSession.organization_id == organization_id,
                        ChatSession.created_at < chat_cutoff,
                        or_(
                            ChatSession.customer_name.is_not(None),
                            ChatSession.customer_email.is_not(None),
                            ChatSession.id.in_(
                                select(ChatMessage.session_id).where(ChatMessage.body != "")
                            ),
                        ),
                    )
                    .order_by(ChatSession.created_at, ChatSession.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        chat_messages = await self._redact_chat_sessions(chat_ids)

        email_ids = list(
            (
                await self._db_session.scalars(
                    select(EmailWorkItem.id)
                    .where(
                        EmailWorkItem.organization_id == organization_id,
                        EmailWorkItem.received_at < email_cutoff,
                        or_(
                            EmailWorkItem.body != "",
                            EmailWorkItem.subject != "",
                            EmailWorkItem.sender != "",
                            EmailWorkItem.draft_body.is_not(None),
                        ),
                    )
                    .order_by(EmailWorkItem.received_at, EmailWorkItem.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        email_items, email_drafts = await self._redact_email_items(email_ids)

        audit_ids = list(
            (
                await self._db_session.scalars(
                    select(AuditEvent.id)
                    .where(
                        AuditEvent.organization_id == organization_id,
                        AuditEvent.occurred_at < audit_cutoff,
                    )
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                    .limit(batch_size)
                )
            ).all()
        )
        deleted_audit_ids: list[UUID] = []
        if audit_ids:
            deleted_audit_ids = list(
                (
                    await self._db_session.scalars(
                        delete(AuditEvent)
                        .where(AuditEvent.id.in_(audit_ids))
                        .returning(AuditEvent.id)
                    )
                ).all()
            )
        result = RetentionResult(
            chat_messages=chat_messages,
            email_items=email_items,
            email_drafts=email_drafts,
            audit_events=len(deleted_audit_ids),
        )
        await self._db_session.flush()
        return result

    async def _policy_for_organization(self, organization_id: UUID) -> RetentionPolicy:
        policy = await self._db_session.scalar(
            select(RetentionPolicy).where(RetentionPolicy.organization_id == organization_id)
        )
        if not isinstance(policy, RetentionPolicy):
            raise LookupError("retention policy not found")
        return policy

    async def _require_policy_action(
        self, principal: Principal, policy: RetentionPolicy, action: Action
    ) -> None:
        try:
            await self._authorization.require(
                principal,
                action,
                ResourceRef(
                    organization_id=policy.organization_id,
                    resource_type="retention",
                    resource_id=policy.id,
                    state=ResourceState.ACTIVE,
                ),
            )
        except AuthorizationDenied:
            raise RetentionAuthorizationError from None

    @staticmethod
    def _require_admin(principal: Principal) -> None:
        if principal.role is not UserRole.ADMIN:
            raise RetentionAuthorizationError

    async def _redact_chat_sessions(self, session_ids: list[UUID]) -> int:
        if not session_ids:
            return 0
        message_ids = list(
            (
                await self._db_session.scalars(
                    select(ChatMessage.id).where(
                        ChatMessage.session_id.in_(session_ids), ChatMessage.body != ""
                    )
                )
            ).all()
        )
        await self._db_session.execute(
            update(ChatSession)
            .where(ChatSession.id.in_(session_ids))
            .values(customer_name=None, customer_email=None, updated_at=func.clock_timestamp())
        )
        if message_ids:
            await self._db_session.execute(
                update(ChatMessage).where(ChatMessage.id.in_(message_ids)).values(body="")
            )
        await self._db_session.execute(
            update(Handoff)
            .where(Handoff.session_id.in_(session_ids))
            .values(snapshot={"erased": True}, updated_at=func.clock_timestamp())
        )
        await self._db_session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.aggregate_type == "chat_session",
                OutboxEvent.aggregate_id.in_(session_ids),
            )
            .values(payload={"erased": True})
        )
        return len(message_ids)

    async def _redact_email_items(self, item_ids: list[UUID]) -> tuple[int, int]:
        if not item_ids:
            return 0, 0
        draft_ids = list(
            (
                await self._db_session.scalars(
                    select(EmailDraftVersion.id).where(
                        EmailDraftVersion.work_item_id.in_(item_ids),
                        or_(
                            EmailDraftVersion.body != "",
                            EmailDraftVersion.subject != "",
                            EmailDraftVersion.reviewer_instruction.is_not(None),
                        ),
                    )
                )
            ).all()
        )
        await self._db_session.execute(
            update(EmailWorkItem)
            .where(EmailWorkItem.id.in_(item_ids))
            .values(
                sender="",
                recipients=[],
                subject="",
                body="",
                raw_content_ref="",
                classification_provenance={},
                draft_body=None,
                draft_citations=[],
                draft_provenance={},
                updated_at=func.clock_timestamp(),
            )
        )
        if draft_ids:
            await self._db_session.execute(
                update(EmailDraftVersion)
                .where(EmailDraftVersion.id.in_(draft_ids))
                .values(
                    {
                        EmailDraftVersion.body: "",
                        EmailDraftVersion.to: [],
                        EmailDraftVersion.cc: [],
                        EmailDraftVersion.subject: "",
                        EmailDraftVersion.reviewer_instruction: None,
                        EmailDraftVersion.retrieval_config: {},
                        EmailDraftVersion.citations: [],
                    }
                )
            )
        await self._db_session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.aggregate_type == "email_work_item",
                OutboxEvent.aggregate_id.in_(item_ids),
            )
            .values(payload={"erased": True})
        )
        return len(item_ids), len(draft_ids)


class ErasureService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        hash_key: bytes,
        principal: Principal | None = None,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
        job_service: JobService | None = None,
    ) -> None:
        self._db_session = db_session
        self._hash_key = hash_key
        self._principal = principal
        self._retention = RetentionService(db_session)
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()
        self._jobs = job_service or JobService()

    async def request(self, subject_ref: str, scope: ErasureScope) -> ErasureRequest:
        principal = self._required_principal()
        policy = await self._retention.policy(principal)
        await self._retention._require_policy_action(principal, policy, "retention.erase")
        digest = subject_key_hash(self._hash_key, subject_ref)
        request = ErasureRequest(
            organization_id=principal.organization_id,
            requested_by_id=principal.subject_id,
            subject_key_hash=digest,
            scope=scope,
            status=ErasureStatus.PENDING,
        )
        self._db_session.add(request)
        await self._db_session.flush()
        await self._capture_targets(request, subject_ref)
        job = await self._jobs.enqueue(
            self._db_session,
            "retention.erasure.apply",
            f"retention-erasure:{request.id}",
            {
                "organization_id": str(request.organization_id),
                "erasure_request_id": str(request.id),
            },
        )
        await self._record_requested(principal, request, job_id=job.id)
        await self._db_session.flush()
        return request

    async def apply(
        self,
        request_id: UUID,
        *,
        restore_generation: int | None = None,
        force: bool = False,
    ) -> ErasureRequest:
        request = await self._db_session.scalar(
            select(ErasureRequest).where(ErasureRequest.id == request_id).with_for_update()
        )
        if not isinstance(request, ErasureRequest):
            raise LookupError("erasure request not found")
        if self._principal is not None:
            if request.organization_id != self._principal.organization_id:
                raise RetentionAuthorizationError
            policy = await self._retention.policy(self._principal)
            await self._retention._require_policy_action(self._principal, policy, "retention.erase")
        if request.status is ErasureStatus.APPLIED and not force and restore_generation is None:
            return request
        targets = list(
            (
                await self._db_session.scalars(
                    select(ErasureTarget)
                    .where(ErasureTarget.request_id == request.id)
                    .order_by(ErasureTarget.target_type, ErasureTarget.target_id)
                )
            ).all()
        )
        counts: dict[str, object] = {
            "chat_sessions": 0,
            "email_items": 0,
            "knowledge_documents": 0,
        }
        chat_ids = [
            target.target_id
            for target in targets
            if target.target_type is ErasureTargetType.CHAT_SESSION
        ]
        email_ids = [
            target.target_id
            for target in targets
            if target.target_type is ErasureTargetType.EMAIL_WORK_ITEM
        ]
        document_ids = [
            target.target_id
            for target in targets
            if target.target_type is ErasureTargetType.KNOWLEDGE_DOCUMENT
        ]
        if chat_ids:
            await self._retention._redact_chat_sessions(chat_ids)
            counts["chat_sessions"] = len(chat_ids)
        if email_ids:
            await self._retention._redact_email_items(email_ids)
            counts["email_items"] = len(email_ids)
        if document_ids:
            await self._db_session.execute(
                update(Document)
                .where(Document.id.in_(document_ids))
                .values(current_version_id=None)
            )
            await self._db_session.flush()
            deleted_document_ids = list(
                (
                    await self._db_session.scalars(
                        delete(Document)
                        .where(
                            Document.id.in_(document_ids),
                            Document.organization_id == request.organization_id,
                        )
                        .returning(Document.id)
                    )
                ).all()
            )
            counts["knowledge_documents"] = len(deleted_document_ids)
        request.status = ErasureStatus.APPLIED
        request.applied_at = func.clock_timestamp()
        request.replay_generation = max(request.replay_generation, restore_generation or 0)
        request.verification_counts = counts
        request.last_error_code = None
        await self._record_applied(request)
        await self._db_session.flush()
        return request

    async def replay_pending_and_applied(self, *, restore_generation: int) -> int:
        if restore_generation <= 0:
            raise ValueError("restore generation must be positive")
        request_ids = list(
            (
                await self._db_session.scalars(
                    select(ErasureRequest.id)
                    .where(ErasureRequest.replay_generation < restore_generation)
                    .order_by(ErasureRequest.requested_at, ErasureRequest.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for request_id in request_ids:
            await self.apply(
                request_id,
                restore_generation=restore_generation,
                force=True,
            )
        return len(request_ids)

    async def replay_is_complete(self, *, restore_generation: int) -> bool:
        if restore_generation <= 0:
            return False
        remaining = await self._db_session.scalar(
            select(func.count(ErasureRequest.id)).where(
                or_(
                    ErasureRequest.status != ErasureStatus.APPLIED,
                    ErasureRequest.replay_generation < restore_generation,
                )
            )
        )
        return int(remaining or 0) == 0

    async def _capture_targets(self, request: ErasureRequest, subject_ref: str) -> None:
        target_pairs: list[tuple[ErasureTargetType, UUID]] = []
        if request.scope is ErasureScope.CUSTOMER:
            normalized = subject_ref.strip().casefold()
            chat_ids = list(
                (
                    await self._db_session.scalars(
                        select(ChatSession.id).where(
                            ChatSession.organization_id == request.organization_id,
                            func.lower(ChatSession.customer_email) == normalized,
                        )
                    )
                ).all()
            )
            email_ids = list(
                (
                    await self._db_session.scalars(
                        select(EmailWorkItem.id).where(
                            EmailWorkItem.organization_id == request.organization_id,
                            func.lower(EmailWorkItem.sender) == normalized,
                        )
                    )
                ).all()
            )
            target_pairs.extend((ErasureTargetType.CHAT_SESSION, item) for item in chat_ids)
            target_pairs.extend((ErasureTargetType.EMAIL_WORK_ITEM, item) for item in email_ids)
        else:
            try:
                document_id = UUID(subject_ref.strip())
            except ValueError as error:
                raise ValueError("knowledge erasure subject must be a document UUID") from error
            document = await self._db_session.scalar(
                select(Document).where(
                    Document.id == document_id,
                    Document.organization_id == request.organization_id,
                )
            )
            if not isinstance(document, Document):
                raise LookupError("knowledge document not found")
            principal = self._required_principal()
            try:
                await AuthorizationService(self._db_session).require(
                    principal,
                    "knowledge.write",
                    ResourceRef(
                        organization_id=document.organization_id,
                        resource_type="knowledge",
                        resource_id=document.knowledge_base_id,
                        state=ResourceState.ACTIVE,
                    ),
                )
            except AuthorizationDenied:
                raise RetentionAuthorizationError from None
            target_pairs.append((ErasureTargetType.KNOWLEDGE_DOCUMENT, document.id))
        for target_type, target_id in target_pairs:
            await self._db_session.execute(
                insert(ErasureTarget)
                .values(request_id=request.id, target_type=target_type, target_id=target_id)
                .on_conflict_do_nothing(
                    index_elements=[
                        ErasureTarget.request_id,
                        ErasureTarget.target_type,
                        ErasureTarget.target_id,
                    ]
                )
            )

    async def _record_requested(
        self, principal: Principal, request: ErasureRequest, *, job_id: UUID
    ) -> None:
        details = {
            "scope": request.scope.value,
            "status": request.status.value,
            "job_id": str(job_id),
        }
        await self._audit.record(
            self._db_session,
            principal,
            action="retention.erasure.request",
            object_type="erasure_request",
            object_id=request.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            "retention.erasure.requested",
            "job",
            job_id,
            {
                "organization_id": str(request.organization_id),
                "erasure_request_id": str(request.id),
                **details,
            },
        )

    async def _record_applied(self, request: ErasureRequest) -> None:
        details: dict[str, object] = {
            "scope": request.scope.value,
            "status": request.status.value,
            "replay_generation": request.replay_generation,
            "verification_counts": request.verification_counts,
        }
        actor_id = (
            self._principal.subject_id if self._principal is not None else request.requested_by_id
        )
        await self._audit.record_actor(
            self._db_session,
            organization_id=request.organization_id,
            actor_id=actor_id,
            action="retention.erasure.apply",
            object_type="erasure_request",
            object_id=request.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            "retention.erasure.applied",
            "erasure_request",
            request.id,
            {"organization_id": str(request.organization_id), **details},
        )

    def _required_principal(self) -> Principal:
        if self._principal is None:
            raise RetentionAuthorizationError
        return self._principal


def _policy_values(policy: RetentionPolicy) -> dict[str, int]:
    return {
        "chat_days": policy.chat_days,
        "email_days": policy.email_days,
        "audit_days": policy.audit_days,
        "version": policy.version,
    }
