import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker
from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.email.gmail_gateway import (
    GmailAmbiguousDeliveryError,
    GmailDefinitiveDeliveryError,
    GmailGatewayFactory,
    GmailSendResult,
)
from app.modules.email.models import (
    DeliveryAttempt,
    DeliveryIntent,
    EmailAction,
    EmailApproval,
    EmailDraftVersion,
    EmailState,
    EmailStateHistory,
    EmailWorkItem,
    SuccessfulDelivery,
)
from app.modules.email.state_machine import transition
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService
from app.modules.outbox.service import OutboxService

_MESSAGE_ID_DOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
EMAIL_DELIVERY_KIND = "email.delivery"
EMAIL_DELIVERY_LEASE_SECONDS = 300


class GmailDeliveryGateway(Protocol):
    async def send_raw(self, raw_message: bytes, *, thread_id: str) -> GmailSendResult: ...


class ReconciliationRequired(RuntimeError):
    pass


class EmailDeliveryAuthorizationError(PermissionError):
    pass


class EmailDeliveryConflict(RuntimeError):
    def __init__(self, intent: DeliveryIntent) -> None:
        super().__init__(f"delivery is {intent.state.value} at version {intent.version}")
        self.state = intent.state
        self.version = intent.version


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    id: UUID
    work_item_id: UUID
    state: EmailState
    version: int


def deterministic_message_id(delivery_intent_id: UUID, domain: str) -> str:
    if not _MESSAGE_ID_DOMAIN.fullmatch(domain):
        raise ValueError("invalid Message-ID domain")
    return f"<delivery-{delivery_intent_id}@{domain.lower()}>"


async def queue_approved_email(
    db_session: AsyncSession,
    item: EmailWorkItem,
    draft: EmailDraftVersion,
    approval: EmailApproval,
    principal: Principal,
    *,
    message_id_domain: str = "mail.invalid",
    job_service: JobService | None = None,
    audit_service: AuditService | None = None,
    outbox_service: OutboxService | None = None,
) -> DeliveryIntent:
    existing = await db_session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.approved_draft_version_id == draft.id)
    )
    if existing is not None:
        return existing
    intent_id = uuid4()
    job = await (job_service or JobService()).enqueue(
        db_session,
        EMAIL_DELIVERY_KIND,
        f"email.delivery:{intent_id}",
        {
            "delivery_intent_id": str(intent_id),
            "organization_id": str(item.organization_id),
            "work_item_id": str(item.id),
        },
    )
    intent = DeliveryIntent(
        id=intent_id,
        organization_id=item.organization_id,
        work_item_id=item.id,
        approved_draft_version_id=draft.id,
        approval_id=approval.id,
        job_id=job.id,
        deterministic_message_id=deterministic_message_id(intent_id, message_id_domain),
        state=EmailState.SEND_PENDING,
    )
    db_session.add(intent)
    before = item.state
    item.state = transition(before, EmailAction.QUEUE_SEND)
    item.version += 1
    db_session.add(
        EmailStateHistory(
            work_item_id=item.id,
            organization_id=item.organization_id,
            from_state=before,
            to_state=item.state,
            action=EmailAction.QUEUE_SEND,
            actor_id=principal.subject_id,
            actor_type="STAFF",
            resource_version=item.version,
        )
    )
    await db_session.flush()
    details = {
        "delivery_intent_id": str(intent.id),
        "approved_draft_version_id": str(draft.id),
        "state": intent.state.value,
    }
    await (audit_service or AuditService()).record(
        db_session,
        principal,
        action="email.delivery.queue",
        object_type="email_delivery_intent",
        object_id=intent.id,
        outcome="SUCCESS",
        details=details,
        safe_detail_keys=set(details),
    )
    await (outbox_service or OutboxService()).add(
        db_session,
        "email.delivery.requested",
        "email_delivery_intent",
        intent.id,
        {"organization_id": str(item.organization_id), **details},
    )
    return intent


async def cancel_pending_delivery_for_draft(
    db_session: AsyncSession, work_item_id: UUID, draft_id: UUID
) -> None:
    intent = await db_session.scalar(
        select(DeliveryIntent)
        .where(
            DeliveryIntent.work_item_id == work_item_id,
            DeliveryIntent.approved_draft_version_id == draft_id,
            DeliveryIntent.state == EmailState.SEND_PENDING,
        )
        .with_for_update()
    )
    if intent is None:
        return
    intent.state = EmailState.FAILED_TERMINAL
    intent.last_error_code = "EMAIL_APPROVAL_INVALIDATED"
    intent.version += 1
    await db_session.execute(
        update(JobIntent)
        .where(
            JobIntent.id == intent.job_id,
            JobIntent.state == JobState.PENDING,
        )
        .values(
            state=JobState.FAILED,
            last_error_code="EMAIL_APPROVAL_INVALIDATED",
            error_class=ErrorClass.NON_RETRYABLE,
            version=JobIntent.version + 1,
            updated_at=func.clock_timestamp(),
        )
    )
    await db_session.flush()


class EmailDeliveryService:
    def __init__(
        self,
        db_session: AsyncSession | None,
        gateway: GmailDeliveryGateway | None = None,
        *,
        worker_id: str,
        connector_service: ConnectorService | None = None,
        gateway_factory: GmailGatewayFactory | None = None,
        lease_seconds: int = EMAIL_DELIVERY_LEASE_SECONDS,
        authorization_service: AuthorizationService | None = None,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._gateway = gateway
        self._worker_id = worker_id
        self._connector_service = connector_service
        self._gateway_factory = gateway_factory
        self._lease_seconds = lease_seconds
        self._authorization = authorization_service
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()

    @classmethod
    def from_session_factory(
        cls, gateway: GmailDeliveryGateway, *, worker_id: str
    ) -> "EmailDeliveryService":
        return cls(None, gateway, worker_id=worker_id)

    async def send(self, job_id: UUID) -> EmailDeliveryResult | None:
        if self._db_session is None:
            async with async_sessionmaker() as db_session:
                return await EmailDeliveryService(
                    db_session,
                    self._gateway,
                    worker_id=self._worker_id,
                    connector_service=self._connector_service,
                    gateway_factory=self._gateway_factory,
                    lease_seconds=self._lease_seconds,
                    audit_service=self._audit,
                    outbox_service=self._outbox,
                ).send(job_id)
        db_session = self._db_session
        job = await db_session.get(JobIntent, job_id)
        if job is None or job.kind != EMAIL_DELIVERY_KIND:
            raise LookupError("email delivery job not found")
        intent = await self._intent_for_job(job)
        if intent.state is EmailState.DELIVERY_UNKNOWN:
            raise ReconciliationRequired(intent.id)
        if intent.state is EmailState.SENT:
            return None
        claimed_job = await JobLeaseService(db_session).claim(
            job.id, self._worker_id, self._lease_seconds
        )
        if claimed_job is None:
            await db_session.rollback()
            return None
        await db_session.commit()

        intent = await self._intent_for_job(claimed_job)
        if intent.state is EmailState.SENDING:
            return await self._mark_unknown_without_resend(
                intent, claimed_job, error_code="GMAIL_PREVIOUS_ATTEMPT_UNCERTAIN"
            )
        if intent.state is EmailState.SEND_RETRY_WAIT:
            intent = await self._prepare_automatic_retry(intent, claimed_job)
        intent, item, draft, attempt = await self._claim_intent(intent, claimed_job)
        await db_session.commit()
        try:
            gateway = await self._resolve_gateway(item)
            raw_message = _build_mime(draft, intent.deterministic_message_id)
            # Keep the database-authoritative claim locked from the final
            # provider-I/O fence through result publication.  A concurrent
            # expired-lease takeover can therefore happen either before this
            # boundary (and reject this worker) or after its transaction, but
            # never between validation and the Gmail call.
            await self._lock_active_delivery_job(claimed_job)
        except JobLeaseLost:
            await db_session.rollback()
            raise
        except Exception:
            return await self._record_failure(
                intent,
                item,
                attempt,
                claimed_job,
                error_code="GMAIL_PRE_SEND_FAILURE",
            )
        try:
            provider_result = await gateway.send_raw(raw_message, thread_id=draft.thread_id)
        except GmailDefinitiveDeliveryError as error:
            return await self._record_failure(
                intent, item, attempt, claimed_job, error_code=error.error_code
            )
        except GmailAmbiguousDeliveryError as error:
            return await self._record_unknown(
                intent, item, attempt, claimed_job, error_code=error.error_code
            )
        except Exception:
            return await self._record_unknown(
                intent,
                item,
                attempt,
                claimed_job,
                error_code="GMAIL_RESPONSE_AMBIGUOUS",
            )
        return await self._record_success(intent, item, attempt, claimed_job, provider_result)

    async def request_retry(
        self,
        delivery_intent_id: UUID,
        principal: Principal,
        *,
        expected_version: int | None = None,
    ) -> EmailDeliveryResult:
        if self._db_session is None:
            raise RuntimeError("manual retry requires a request database session")
        locked_intent = await self._db_session.scalar(
            select(DeliveryIntent)
            .where(
                DeliveryIntent.id == delivery_intent_id,
                DeliveryIntent.organization_id == principal.organization_id,
            )
            .with_for_update()
        )
        if locked_intent is None:
            raise LookupError("email delivery intent not found")
        intent = locked_intent
        if expected_version is not None and intent.version != expected_version:
            raise EmailDeliveryConflict(intent)
        if intent.state is EmailState.DELIVERY_UNKNOWN:
            raise ReconciliationRequired(intent.id)
        if intent.state is not EmailState.SEND_RETRY_WAIT:
            raise ValueError("delivery is not awaiting a safe retry")
        item = await self._locked_item(intent.work_item_id)
        await self._require_staff_access(item, principal)
        await self._transition(
            intent,
            item,
            EmailAction.RETRY_SEND,
            actor_id=principal.subject_id,
            actor_type="STAFF",
        )
        updated_job_id = await self._db_session.scalar(
            update(JobIntent)
            .where(
                JobIntent.id == intent.job_id,
                JobIntent.state == JobState.PENDING,
            )
            .values(next_attempt_at=None, last_error_code=None, error_class=None)
            .returning(JobIntent.id)
        )
        if updated_job_id is None:
            raise EmailDeliveryConflict(intent)
        await self._record_event(
            intent,
            item,
            actor_id=principal.subject_id,
            action="email.delivery.retry",
            event_type="email.delivery.retry_requested",
        )
        return _result(intent)

    async def _prepare_automatic_retry(
        self, intent: DeliveryIntent, job: JobIntent
    ) -> DeliveryIntent:
        assert self._db_session is not None
        await self._lock_active_delivery_job(job)
        locked_intent = await self._db_session.scalar(
            select(DeliveryIntent)
            .where(
                DeliveryIntent.id == intent.id,
                DeliveryIntent.state == EmailState.SEND_RETRY_WAIT,
            )
            .with_for_update()
        )
        if locked_intent is None:
            raise EmailDeliveryConflict(await self._intent_for_job(job))
        intent = locked_intent
        item = await self._locked_item(intent.work_item_id)
        await self._transition(
            intent,
            item,
            EmailAction.RETRY_SEND,
            actor_id=_delivery_worker_actor_id(item.organization_id),
            actor_type="SYSTEM",
            job_id=job.id,
        )
        return intent

    async def _intent_for_job(self, job: JobIntent) -> DeliveryIntent:
        raw_intent_id = job.payload.get("delivery_intent_id")
        try:
            intent_id = UUID(str(raw_intent_id))
        except (TypeError, ValueError) as error:
            raise ValueError("delivery job has invalid intent identity") from error
        intent = await self._db_session.get(DeliveryIntent, intent_id)  # type: ignore[union-attr]
        if intent is None or intent.job_id != job.id:
            raise LookupError("email delivery intent not found")
        return intent

    async def _claim_intent(
        self, intent: DeliveryIntent, job: JobIntent
    ) -> tuple[DeliveryIntent, EmailWorkItem, EmailDraftVersion, DeliveryAttempt]:
        assert self._db_session is not None
        # Lock the live job generation before touching any delivery state.  The
        # lock and all intent/attempt writes commit together, making a lease
        # takeover race-safe rather than an application-level pre-check.
        await self._lock_active_delivery_job(job)
        item = await self._db_session.scalar(
            select(EmailWorkItem).where(EmailWorkItem.id == intent.work_item_id).with_for_update()
        )
        if item is None:
            raise LookupError("email work item not found")
        draft = await self._db_session.get(EmailDraftVersion, intent.approved_draft_version_id)
        approval = await self._db_session.get(EmailApproval, intent.approval_id)
        if (
            draft is None
            or draft.work_item_id != item.id
            or approval is None
            or approval.draft_version_id != draft.id
            or approval.invalidated_at is not None
            or item.current_draft_id != draft.id
            or item.state is not EmailState.SEND_PENDING
        ):
            await self._db_session.rollback()
            await self._fail_job_terminal(job, "EMAIL_APPROVAL_INVALID")
            raise ValueError("approved delivery identity is no longer current")
        claimed = await self._db_session.scalar(
            update(DeliveryIntent)
            .where(
                DeliveryIntent.id == intent.id,
                DeliveryIntent.state == EmailState.SEND_PENDING,
                DeliveryIntent.version == intent.version,
            )
            .values(
                state=EmailState.SENDING,
                attempts=DeliveryIntent.attempts + 1,
                last_error_code=None,
                version=DeliveryIntent.version + 1,
                updated_at=func.clock_timestamp(),
            )
            .returning(DeliveryIntent)
        )
        if claimed is None:
            await self._db_session.rollback()
            raise ReconciliationRequired(intent.id)
        before = item.state
        item.state = transition(before, EmailAction.CLAIM_SEND)
        item.version += 1
        attempt = DeliveryAttempt(
            delivery_intent_id=claimed.id,
            attempt_number=claimed.attempts,
            outcome="IN_PROGRESS",
        )
        self._db_session.add_all(
            (
                attempt,
                EmailStateHistory(
                    work_item_id=item.id,
                    organization_id=item.organization_id,
                    from_state=before,
                    to_state=item.state,
                    action=EmailAction.CLAIM_SEND,
                    actor_id=_delivery_worker_actor_id(item.organization_id),
                    actor_type="SYSTEM",
                    job_id=job.id,
                    resource_version=item.version,
                ),
            )
        )
        await self._db_session.flush()
        return claimed, item, draft, attempt

    async def _lock_active_delivery_job(self, job: JobIntent) -> JobIntent:
        assert self._db_session is not None
        job_id = job.id
        locked = await self._db_session.scalar(
            select(JobIntent)
            .where(
                JobIntent.id == job.id,
                JobIntent.kind == EMAIL_DELIVERY_KIND,
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_owner == self._worker_id,
                JobIntent.version == job.version,
                JobIntent.lease_expires_at.is_not(None),
                JobIntent.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:
            await self._db_session.rollback()
            raise JobLeaseLost(job_id)
        return locked

    async def _record_success(
        self,
        intent: DeliveryIntent,
        item: EmailWorkItem,
        attempt: DeliveryAttempt,
        job: JobIntent,
        provider_result: GmailSendResult,
    ) -> EmailDeliveryResult:
        assert self._db_session is not None
        try:
            await JobLeaseService(self._db_session).complete(
                job.id, self._worker_id, expected_version=job.version
            )
            intent = await self._locked_sending_intent(intent.id)
            item = await self._locked_item(item.id)
            await self._transition(
                intent,
                item,
                EmailAction.SEND_SUCCEEDED,
                actor_id=_delivery_worker_actor_id(item.organization_id),
                actor_type="SYSTEM",
                job_id=job.id,
            )
            stored_attempt = await self._db_session.get(DeliveryAttempt, attempt.id)
            if stored_attempt is None:
                raise RuntimeError("delivery attempt is missing")
            stored_attempt.outcome = "SENT"
            stored_attempt.completed_at = await self._db_session.scalar(
                select(func.clock_timestamp())
            )
            self._db_session.add(
                SuccessfulDelivery(
                    delivery_intent_id=intent.id,
                    gmail_message_id=provider_result.gmail_message_id,
                    gmail_thread_id=provider_result.gmail_thread_id,
                )
            )
            await self._record_event(
                intent,
                item,
                actor_id=_delivery_worker_actor_id(item.organization_id),
                action="email.delivery.sent",
                event_type="email.delivery.sent",
            )
            await self._db_session.commit()
            return _result(intent)
        except JobLeaseLost:
            await self._db_session.rollback()
            raise

    async def _record_failure(
        self,
        intent: DeliveryIntent,
        item: EmailWorkItem,
        attempt: DeliveryAttempt,
        job: JobIntent,
        *,
        error_code: str,
    ) -> EmailDeliveryResult:
        assert self._db_session is not None
        await JobLeaseService(self._db_session).retry(
            job.id,
            self._worker_id,
            error_code=error_code,
            error_class=ErrorClass.RETRYABLE,
            expected_version=job.version,
        )
        intent = await self._locked_sending_intent(intent.id)
        item = await self._locked_item(item.id)
        intent.last_error_code = error_code
        await self._transition(
            intent,
            item,
            EmailAction.SEND_FAILED,
            actor_id=_delivery_worker_actor_id(item.organization_id),
            actor_type="SYSTEM",
            job_id=job.id,
            reason_code=error_code,
        )
        await self._finish_attempt(attempt.id, "DEFINITIVE_FAILURE", error_code)
        await self._record_event(
            intent,
            item,
            actor_id=_delivery_worker_actor_id(item.organization_id),
            action="email.delivery.retry_wait",
            event_type="email.delivery.retry_wait",
        )
        await self._db_session.commit()
        return _result(intent)

    async def _record_unknown(
        self,
        intent: DeliveryIntent,
        item: EmailWorkItem,
        attempt: DeliveryAttempt,
        job: JobIntent,
        *,
        error_code: str,
    ) -> EmailDeliveryResult:
        assert self._db_session is not None
        await JobLeaseService(self._db_session).retry(
            job.id,
            self._worker_id,
            error_code=error_code,
            error_class=ErrorClass.AMBIGUOUS,
            expected_version=job.version,
        )
        intent = await self._locked_sending_intent(intent.id)
        item = await self._locked_item(item.id)
        intent.last_error_code = error_code
        await self._transition(
            intent,
            item,
            EmailAction.DELIVERY_AMBIGUOUS,
            actor_id=_delivery_worker_actor_id(item.organization_id),
            actor_type="SYSTEM",
            job_id=job.id,
            reason_code=error_code,
        )
        await self._finish_attempt(attempt.id, "UNKNOWN", error_code)
        await self._record_event(
            intent,
            item,
            actor_id=_delivery_worker_actor_id(item.organization_id),
            action="email.delivery.unknown",
            event_type="email.delivery.unknown",
        )
        await self._db_session.commit()
        return _result(intent)

    async def _mark_unknown_without_resend(
        self, intent: DeliveryIntent, job: JobIntent, *, error_code: str
    ) -> EmailDeliveryResult:
        assert self._db_session is not None
        item = await self._locked_item(intent.work_item_id)
        return await self._record_unknown(
            intent,
            item,
            await self._latest_attempt(intent.id),
            job,
            error_code=error_code,
        )

    async def _fail_job_terminal(self, job: JobIntent, error_code: str) -> None:
        assert self._db_session is not None
        claimed = await self._db_session.get(JobIntent, job.id)
        if claimed is None:
            return
        await JobLeaseService(self._db_session).retry(
            claimed.id,
            self._worker_id,
            error_code=error_code,
            error_class=ErrorClass.NON_RETRYABLE,
            expected_version=claimed.version,
        )
        await self._db_session.commit()

    async def _resolve_gateway(self, item: EmailWorkItem) -> GmailDeliveryGateway:
        if self._gateway is not None:
            return self._gateway
        if self._connector_service is None or self._gateway_factory is None:
            raise RuntimeError("encrypted Gmail connector and delivery gateway are required")
        assert self._db_session is not None
        connector = await self._db_session.get(Connector, item.connector_id)
        if (
            connector is None
            or connector.organization_id != item.organization_id
            or connector.kind is not ConnectorKind.GMAIL
            or connector.status is not ConnectorStatus.ACTIVE
        ):
            raise RuntimeError("active Gmail connector is unavailable")
        refresh_token = await self._connector_service.load_refresh_token(
            self._db_session, connector
        )
        return (await self._gateway_factory.create(refresh_token=refresh_token)).gateway

    async def _require_staff_access(self, item: EmailWorkItem, principal: Principal) -> None:
        if principal.role not in {UserRole.ADMIN, UserRole.REVIEWER}:
            raise EmailDeliveryAuthorizationError
        assert self._db_session is not None
        authorization = self._authorization or AuthorizationService(self._db_session)
        try:
            await authorization.require(
                principal,
                "knowledge.review",
                ResourceRef(
                    organization_id=item.organization_id,
                    resource_type="knowledge",
                    resource_id=item.knowledge_base_id,
                    state=ResourceState.ACTIVE,
                ),
            )
        except AuthorizationDenied:
            raise EmailDeliveryAuthorizationError from None

    async def _locked_sending_intent(self, intent_id: UUID) -> DeliveryIntent:
        assert self._db_session is not None
        intent = await self._db_session.scalar(
            select(DeliveryIntent)
            .where(
                DeliveryIntent.id == intent_id,
                DeliveryIntent.state == EmailState.SENDING,
            )
            .with_for_update()
        )
        if intent is None:
            raise JobLeaseLost(intent_id)
        return intent

    async def _locked_item(self, item_id: UUID) -> EmailWorkItem:
        assert self._db_session is not None
        item = await self._db_session.scalar(
            select(EmailWorkItem).where(EmailWorkItem.id == item_id).with_for_update()
        )
        if item is None:
            raise LookupError("email work item not found")
        return item

    async def _latest_attempt(self, intent_id: UUID) -> DeliveryAttempt:
        assert self._db_session is not None
        attempt = await self._db_session.scalar(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_intent_id == intent_id)
            .order_by(DeliveryAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt is None:
            raise RuntimeError("delivery attempt is missing")
        return attempt

    async def _finish_attempt(self, attempt_id: UUID, outcome: str, error_code: str) -> None:
        assert self._db_session is not None
        attempt = await self._db_session.get(DeliveryAttempt, attempt_id)
        if attempt is None:
            raise RuntimeError("delivery attempt is missing")
        attempt.outcome = outcome
        attempt.error_code = error_code
        attempt.completed_at = await self._db_session.scalar(select(func.clock_timestamp()))

    async def _transition(
        self,
        intent: DeliveryIntent,
        item: EmailWorkItem,
        action: EmailAction,
        *,
        actor_id: UUID,
        actor_type: str,
        job_id: UUID | None = None,
        reason_code: str | None = None,
    ) -> None:
        before = intent.state
        next_state = transition(before, action)
        if item.state is not before:
            raise RuntimeError("delivery intent and email work item states diverged")
        intent.state = next_state
        intent.version += 1
        item.state = next_state
        item.version += 1
        assert self._db_session is not None
        self._db_session.add(
            EmailStateHistory(
                work_item_id=item.id,
                organization_id=item.organization_id,
                from_state=before,
                to_state=next_state,
                action=action,
                reason_code=reason_code,
                actor_id=actor_id,
                actor_type=actor_type,
                job_id=job_id,
                resource_version=item.version,
            )
        )
        await self._db_session.flush()

    async def _record_event(
        self,
        intent: DeliveryIntent,
        item: EmailWorkItem,
        *,
        actor_id: UUID,
        action: str,
        event_type: str,
    ) -> None:
        assert self._db_session is not None
        details = {
            "delivery_intent_id": str(intent.id),
            "approved_draft_version_id": str(intent.approved_draft_version_id),
            "state": intent.state.value,
            "version": intent.version,
            "error_code": intent.last_error_code,
        }
        await self._audit.record_actor(
            self._db_session,
            organization_id=item.organization_id,
            actor_id=actor_id,
            action=action,
            object_type="email_delivery_intent",
            object_id=intent.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            event_type,
            "email_delivery_intent",
            intent.id,
            {"organization_id": str(item.organization_id), **details},
        )


def _build_mime(draft: EmailDraftVersion, message_id: str) -> bytes:
    message = EmailMessage()
    message["To"] = ", ".join(draft.to)
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    message["Subject"] = draft.subject
    message["Message-ID"] = message_id
    message.set_content(draft.body)
    return message.as_bytes(policy=SMTP)


def _delivery_worker_actor_id(organization_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"email-delivery-worker:{organization_id}")


def _result(intent: DeliveryIntent) -> EmailDeliveryResult:
    return EmailDeliveryResult(intent.id, intent.work_item_id, intent.state, intent.version)
