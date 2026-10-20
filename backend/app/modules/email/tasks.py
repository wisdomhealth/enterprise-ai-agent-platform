"""Celery consumers for durable Gmail history, classification, and draft intents."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import String, and_, delete, exists, func, not_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, DisconnectionError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import celery_async_sessionmaker as async_sessionmaker
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.email.actors import (
    EMAIL_SYSTEM_ACTOR_TYPE,
    email_worker_actor_id,
    email_worker_principal,
)
from app.modules.email.classification import (
    EmailClassificationFailure,
    EmailClassifier,
    EmailClassifierConfigurationError,
    OpenAIEmailClassifier,
)
from app.modules.email.delivery import EMAIL_DELIVERY_KIND, EmailDeliveryService
from app.modules.email.drafting import EmailDraftFailure, EmailDraftingService
from app.modules.email.gmail_gateway import GmailTransientError, GoogleGmailGatewayFactory
from app.modules.email.ingestion import EmailIngestionService
from app.modules.email.models import (
    DeliveryIntent,
    EmailAction,
    EmailState,
    EmailStateHistory,
    EmailSyncState,
    EmailWorkItem,
    GmailPushReceipt,
    GmailSyncStatus,
)
from app.modules.email.sync import (
    EMAIL_HISTORY_KIND,
    EMAIL_HISTORY_OUTBOX_EVENT_TYPE,
    GmailSyncCoordinator,
    GmailSyncPaused,
)
from app.modules.email.watch import GmailWatchService
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService, RetryPolicy
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent, ProcessedEvent
from app.modules.outbox.service import OutboxService
from app.modules.rag.answer_service import GroundedAnswerService

EMAIL_CLASSIFY_KIND = "email.classify"
EMAIL_DRAFT_KIND = "email.draft"
EMAIL_JOB_TASK_NAME = "app.modules.email.tasks.email_job"
EMAIL_DELIVERY_OUTBOX_TASK_NAME = "app.modules.email.tasks.email_delivery_outbox_event"
EMAIL_DELIVERY_OUTBOX_CONSUMER = "email-delivery-requested-v1"
EMAIL_DELIVERY_OUTBOX_EVENT_TYPES = frozenset(
    {"email.delivery.requested", "email.delivery.retry_requested"}
)
EMAIL_WORKER_ID = "celery-email"
EMAIL_LEASE_SECONDS = 300
logger = logging.getLogger(__name__)


class _UnavailableEmailClassifier:
    async def classify(self, _subject: str, _body: str):  # type: ignore[no-untyped-def]
        raise EmailClassifierConfigurationError("OPENAI_API_KEY is not configured")


@shared_task(name=EMAIL_JOB_TASK_NAME)  # type: ignore[untyped-decorator]
def email_job(job_id: str) -> None:
    asyncio.run(_consume_email_job(UUID(job_id)))


@shared_task(  # type: ignore[untyped-decorator]
    name=EMAIL_DELIVERY_OUTBOX_TASK_NAME,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def email_delivery_outbox_event(event_id: str) -> None:
    """Consume one durable delivery request with bounded broker retries."""
    asyncio.run(_consume_email_delivery_outbox_event(UUID(event_id)))


@shared_task(  # type: ignore[untyped-decorator]
    name="app.modules.email.tasks.dispatch_pending_email_delivery_outbox_events"
)
def dispatch_pending_email_delivery_outbox_events() -> None:
    """Redeliver durable email intents left pending across restart/failure."""
    asyncio.run(_dispatch_pending_email_delivery_outbox_events())


@shared_task(  # type: ignore[untyped-decorator]
    name="app.modules.email.tasks.dispatch_pending_email_history_outbox_events"
)
def dispatch_pending_email_history_outbox_events() -> None:
    """Publish committed Gmail-history jobs without polling Gmail."""
    asyncio.run(_dispatch_pending_email_history_outbox_events())


@shared_task(name="app.modules.email.tasks.dispatch_pending_email_jobs")  # type: ignore[untyped-decorator]
def dispatch_pending_email_jobs() -> None:
    asyncio.run(_dispatch_pending_email_jobs())


@shared_task(name="app.modules.email.tasks.renew_gmail_watches")  # type: ignore[untyped-decorator]
def renew_gmail_watches() -> None:
    asyncio.run(_renew_gmail_watches())


@shared_task(name="app.modules.email.tasks.reconcile_gmail_history")  # type: ignore[untyped-decorator]
def reconcile_gmail_history() -> None:
    asyncio.run(_reconcile_gmail_history())


@shared_task(name="app.modules.email.tasks.cleanup_gmail_push_receipts")  # type: ignore[untyped-decorator]
def cleanup_gmail_push_receipts() -> None:
    asyncio.run(_cleanup_gmail_push_receipts())


async def _enqueue_gmail_history_jobs(
    *,
    db_session: AsyncSession | None = None,
    now: datetime | None = None,
    interval_seconds: int | None = None,
) -> None:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _enqueue_gmail_history_jobs(
                db_session=owned_session,
                now=now,
                interval_seconds=interval_seconds,
            )
        return
    settings = Settings()
    scan_time = now or datetime.now(UTC)
    due_interval = interval_seconds or settings.gmail_reconciliation_interval_seconds
    rows = (
        await db_session.execute(
            select(Connector, KnowledgeBase.id)
            .join(
                KnowledgeBase,
                KnowledgeBase.organization_id == Connector.organization_id,
            )
            .where(
                Connector.kind == ConnectorKind.GMAIL,
                Connector.status == ConnectorStatus.ACTIVE,
            )
        )
    ).all()
    for connector, knowledge_base_id in rows:
        await db_session.execute(
            insert(EmailSyncState)
            .values(
                organization_id=connector.organization_id,
                connector_id=connector.id,
            )
            .on_conflict_do_nothing(index_elements=[EmailSyncState.connector_id])
        )
        sync_state = await db_session.scalar(
            select(EmailSyncState)
            .where(EmailSyncState.connector_id == connector.id)
            .with_for_update()
        )
        if sync_state is None:
            continue
        if (
            sync_state.last_success_at is not None
            and sync_state.last_success_at > scan_time - timedelta(seconds=due_interval)
        ):
            continue
        try:
            await GmailSyncCoordinator(db_session).schedule_locked(
                sync_state,
                knowledge_base_id=knowledge_base_id,
                trigger="SCHEDULED",
            )
        except GmailSyncPaused:
            continue
    await db_session.commit()


async def _reconcile_gmail_history() -> None:
    settings = Settings()
    if not settings.gmail_polling_enabled:
        return
    await _enqueue_gmail_history_jobs(
        interval_seconds=settings.gmail_reconciliation_interval_seconds
    )


async def _renew_gmail_watches() -> None:
    settings = Settings()
    connector_service = ConnectorService.from_settings(settings)
    gateway_factory = GoogleGmailGatewayFactory.from_settings(settings)
    if (
        not settings.gmail_push_enabled
        or connector_service is None
        or gateway_factory is None
        or settings.gmail_pubsub_topic is None
    ):
        return
    threshold = datetime.now(UTC) + timedelta(
        seconds=settings.gmail_watch_renew_before_seconds
    )
    async with async_sessionmaker() as db_session:
        connector_ids = list(
            (
                await db_session.scalars(
                    select(EmailSyncState.connector_id).where(
                        EmailSyncState.auto_sync_enabled.is_(True),
                        or_(
                            EmailSyncState.watch_expiration_at.is_(None),
                            EmailSyncState.watch_expiration_at <= threshold,
                        ),
                    )
                )
            ).all()
        )
    for connector_id in connector_ids:
        async with async_sessionmaker() as db_session:
            organization_id = await db_session.scalar(
                select(EmailSyncState.organization_id).where(
                    EmailSyncState.connector_id == connector_id
                )
            )
            if organization_id is None:
                continue
            try:
                await GmailWatchService(
                    db_session,
                    connector_service=connector_service,
                    gateway_factory=gateway_factory,
                    topic_name=settings.gmail_pubsub_topic,
                ).renew(connector_id, organization_id)
            except Exception:
                logger.exception(
                    "Gmail watch renewal failed",
                    extra={"connector_id": str(connector_id)},
                )


async def _cleanup_gmail_push_receipts() -> int:
    settings = Settings()
    if not settings.gmail_push_enabled:
        return 0
    async with async_sessionmaker() as db_session:
        receipt_ids = list(
            (
                await db_session.scalars(
                    select(GmailPushReceipt.id)
                    .where(GmailPushReceipt.expires_at <= func.clock_timestamp())
                    .order_by(GmailPushReceipt.expires_at)
                    .limit(1000)
                )
            ).all()
        )
        if receipt_ids:
            await db_session.execute(
                delete(GmailPushReceipt).where(GmailPushReceipt.id.in_(receipt_ids))
            )
            await db_session.commit()
        return len(receipt_ids)


async def _dispatch_pending_email_jobs() -> None:
    async with async_sessionmaker() as db_session:
        unpublished_history_event = exists(
            select(OutboxEvent.event_id).where(
                OutboxEvent.event_type == EMAIL_HISTORY_OUTBOX_EVENT_TYPE,
                OutboxEvent.aggregate_type == "job",
                OutboxEvent.aggregate_id == JobIntent.id,
                OutboxEvent.payload["job_id"].astext == func.cast(JobIntent.id, String),
                OutboxEvent.published_at.is_(None),
            )
        )
        job_ids = list(
            (
                await db_session.scalars(
                    select(JobIntent.id).where(
                        JobIntent.kind.in_(
                            (
                                EMAIL_HISTORY_KIND,
                                EMAIL_CLASSIFY_KIND,
                                EMAIL_DRAFT_KIND,
                                EMAIL_DELIVERY_KIND,
                            )
                        ),
                        or_(
                            and_(
                                JobIntent.state == JobState.PENDING,
                                not_(
                                    and_(
                                        JobIntent.kind == EMAIL_HISTORY_KIND,
                                        unpublished_history_event,
                                    )
                                ),
                                or_(
                                    JobIntent.next_attempt_at.is_(None),
                                    JobIntent.next_attempt_at <= func.clock_timestamp(),
                                ),
                            ),
                            and_(
                                JobIntent.state == JobState.RUNNING,
                                JobIntent.lease_expires_at.is_not(None),
                                JobIntent.lease_expires_at <= func.clock_timestamp(),
                            ),
                        ),
                    )
                )
            ).all()
        )
    for job_id in job_ids:
        email_job.delay(str(job_id))


async def _dispatch_email_history_outbox_event(
    event_id: UUID, *, db_session: AsyncSession | None = None
) -> bool:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            return await _dispatch_email_history_outbox_event(
                event_id, db_session=owned_session
            )
    event = await db_session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.event_id == event_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if (
        event is None
        or event.event_type != EMAIL_HISTORY_OUTBOX_EVENT_TYPE
        or event.aggregate_type != "job"
        or event.published_at is not None
        or event.payload.get("job_id") != str(event.aggregate_id)
    ):
        return False
    job = await db_session.get(JobIntent, event.aggregate_id)
    if job is None or job.kind != EMAIL_HISTORY_KIND:
        return False
    try:
        email_job.delay(str(job.id))
    except Exception:
        event.publish_attempts += 1
        await db_session.commit()
        raise
    event.publish_attempts += 1
    event.published_at = func.clock_timestamp()
    await db_session.commit()
    return True


async def _dispatch_pending_email_history_outbox_events(
    *, db_session: AsyncSession | None = None
) -> None:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _dispatch_pending_email_history_outbox_events(db_session=owned_session)
            return
    event_ids = list(
        (
            await db_session.scalars(
                select(OutboxEvent.event_id)
                .where(
                    OutboxEvent.event_type == EMAIL_HISTORY_OUTBOX_EVENT_TYPE,
                    OutboxEvent.aggregate_type == "job",
                    OutboxEvent.published_at.is_(None),
                )
                .order_by(OutboxEvent.occurred_at, OutboxEvent.event_id)
            )
        ).all()
    )
    for event_id in event_ids:
        await _dispatch_email_history_outbox_event(event_id, db_session=db_session)


async def _dispatch_email_delivery_outbox_event(
    event_id: UUID, *, db_session: AsyncSession | None = None
) -> bool:
    """Publish one durable delivery event and mark it only after broker acceptance."""
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            return await _dispatch_email_delivery_outbox_event(
                event_id, db_session=owned_session
            )
    event = await db_session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.event_id == event_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if (
        event is None
        or event.event_type not in EMAIL_DELIVERY_OUTBOX_EVENT_TYPES
        or event.aggregate_type != "email_delivery_intent"
        or event.published_at is not None
    ):
        return False
    try:
        email_delivery_outbox_event.delay(str(event.event_id))
    except Exception:
        event.publish_attempts += 1
        await db_session.commit()
        raise
    event.publish_attempts += 1
    event.published_at = func.clock_timestamp()
    await db_session.commit()
    return True


async def _dispatch_pending_email_delivery_outbox_events(
    *, db_session: AsyncSession | None = None
) -> None:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _dispatch_pending_email_delivery_outbox_events(db_session=owned_session)
            return
    event_ids = list(
        (
            await db_session.scalars(
                select(OutboxEvent.event_id)
                .where(
                    OutboxEvent.event_type.in_(EMAIL_DELIVERY_OUTBOX_EVENT_TYPES),
                    OutboxEvent.aggregate_type == "email_delivery_intent",
                    OutboxEvent.published_at.is_(None),
                )
                .order_by(OutboxEvent.occurred_at, OutboxEvent.event_id)
            )
        ).all()
    )
    for event_id in event_ids:
        await _dispatch_email_delivery_outbox_event(event_id, db_session=db_session)


async def _consume_email_delivery_outbox_event(event_id: UUID) -> bool:
    """Idempotently connect one Outbox event to the durable delivery consumer."""
    settings = Settings()
    async with async_sessionmaker() as db_session:
        event = await db_session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_id == event_id,
                OutboxEvent.event_type.in_(EMAIL_DELIVERY_OUTBOX_EVENT_TYPES),
                OutboxEvent.aggregate_type == "email_delivery_intent",
            )
        )
        if event is None:
            return False
        first_delivery = await OutboxService().begin_processing(
            db_session, EMAIL_DELIVERY_OUTBOX_CONSUMER, event.event_id
        )
        if not first_delivery:
            await db_session.rollback()
            return False
        intent = await db_session.get(DeliveryIntent, event.aggregate_id)
        if intent is None:
            raise LookupError("email delivery intent not found")
        if event.payload.get("delivery_intent_id") != str(intent.id):
            raise ValueError("delivery Outbox event identity mismatch")
        if event.payload.get("organization_id") != str(intent.organization_id):
            raise ValueError("delivery Outbox organization mismatch")
        await _consume_delivery_job(db_session, intent.job_id, settings)
        # ``EmailDeliveryService`` commits its durable claim before Gmail I/O;
        # on terminal/idempotent paths this explicit commit makes the consumer
        # marker equally durable.  A rolled-back competing claim leaves no
        # false ProcessedEvent and the JobIntent sweep remains authoritative.
        await db_session.commit()
        processed = await db_session.get(
            ProcessedEvent,
            {
                "consumer_name": EMAIL_DELIVERY_OUTBOX_CONSUMER,
                "event_id": event.event_id,
            },
        )
        return processed is not None


async def _consume_email_job(job_id: UUID) -> None:
    settings = Settings()
    async with async_sessionmaker() as db_session:
        pending_job = await db_session.get(JobIntent, job_id)
        if pending_job is not None and pending_job.kind == EMAIL_DELIVERY_KIND:
            await _consume_delivery_job(db_session, job_id, settings)
            return
        lease_service = JobLeaseService(
            db_session,
            retry_policy=RetryPolicy(
                base_seconds=settings.email_classification_retry_base_seconds,
                max_seconds=settings.email_classification_retry_max_seconds,
            ),
        )
        execution_owner = f"{EMAIL_WORKER_ID}:{uuid4()}"
        job = await lease_service.claim(job_id, execution_owner, lease_seconds=EMAIL_LEASE_SECONDS)
        if job is None:
            return
        claimed_job_id = job.id
        claimed_job_kind = job.kind
        claimed_job_payload = dict(job.payload)
        claimed_version = job.version
        await db_session.commit()
        try:
            if claimed_job_kind == EMAIL_HISTORY_KIND:
                result = await _consume_history_with_lease_renewal(
                    db_session,
                    claimed_job_id,
                    claimed_job_payload,
                    settings,
                    execution_owner,
                    claimed_version,
                )
                current_version = await _current_claim_version(
                    db_session, claimed_job_id, execution_owner
                )
                if result:
                    await lease_service.retry(
                        claimed_job_id,
                        execution_owner,
                        error_code="GMAIL_REAUTH_REQUIRED",
                        error_class=ErrorClass.NON_RETRYABLE,
                        expected_version=current_version,
                    )
                    await _pause_history_sync(
                        db_session,
                        claimed_job_id,
                        claimed_job_payload,
                        "GMAIL_REAUTH_REQUIRED",
                    )
                else:
                    await lease_service.complete(
                        claimed_job_id, execution_owner, expected_version=current_version
                    )
                    await _finalize_history_sync(
                        db_session, claimed_job_id, claimed_job_payload
                    )
            elif claimed_job_kind == EMAIL_CLASSIFY_KIND:
                try:
                    await _consume_classification(
                        db_session, claimed_job_id, claimed_job_payload, settings
                    )
                except EmailClassificationFailure as failure:
                    retried = await lease_service.retry(
                        claimed_job_id,
                        execution_owner,
                        error_code=failure.error_code,
                        error_class=failure.error_class,
                        retry_after_seconds=failure.retry_after_seconds,
                        max_attempts=settings.email_classification_max_attempts,
                        expected_version=await _current_claim_version(
                            db_session, claimed_job_id, execution_owner
                        ),
                    )
                    await db_session.commit()
                    logger.warning(
                        "email classification failed",
                        extra={
                            "organization_id": claimed_job_payload.get("organization_id"),
                            "work_item_id": claimed_job_payload.get("work_item_id"),
                            "job_id": str(claimed_job_id),
                            "attempt": retried.attempts,
                            "error_code": failure.error_code,
                            "error_class": failure.error_class.value,
                            "failure_retryable": failure.error_class is ErrorClass.RETRYABLE,
                            "retry_scheduled": retried.state is JobState.PENDING,
                            "terminal_reason": (
                                None
                                if retried.state is JobState.PENDING
                                else (
                                    "attempt_limit"
                                    if failure.error_class is ErrorClass.RETRYABLE
                                    else "non_retryable"
                                )
                            ),
                            "next_attempt_at": (
                                retried.next_attempt_at.isoformat()
                                if retried.next_attempt_at is not None
                                else None
                            ),
                        },
                    )
                    return
                else:
                    await lease_service.complete(
                        claimed_job_id,
                        execution_owner,
                        expected_version=await _current_claim_version(
                            db_session, claimed_job_id, execution_owner
                        ),
                    )
            elif claimed_job_kind == EMAIL_DRAFT_KIND:
                try:
                    await _consume_draft(
                        db_session, claimed_job_id, claimed_job_payload, settings
                    )
                except EmailDraftFailure as failure:
                    draft_job = await db_session.get(JobIntent, claimed_job_id)
                    cycle_start = draft_job.cycle_start_attempts or 0 if draft_job else 0
                    cycle_limit = (
                        draft_job.cycle_attempt_limit
                        if draft_job and draft_job.cycle_attempt_limit is not None
                        else settings.email_draft_max_attempts
                    )
                    retried = await lease_service.retry(
                        claimed_job_id,
                        execution_owner,
                        error_code=failure.error_code,
                        error_class=failure.error_class,
                        max_attempts=cycle_start + cycle_limit,
                        expected_version=await _current_claim_version(
                            db_session, claimed_job_id, execution_owner
                        ),
                    )
                    item = await db_session.get(
                        EmailWorkItem, UUID(str(claimed_job_payload["work_item_id"]))
                    )
                    if item is not None and retried.state is JobState.FAILED:
                        item.active_draft_job_id = None
                else:
                    await lease_service.complete(
                        claimed_job_id,
                        execution_owner,
                        expected_version=await _current_claim_version(
                            db_session, claimed_job_id, execution_owner
                        ),
                    )
                    item = await db_session.get(
                        EmailWorkItem, UUID(str(claimed_job_payload["work_item_id"]))
                    )
                    if item is not None:
                        item.active_draft_job_id = None
            else:
                await lease_service.retry(
                    claimed_job_id,
                    execution_owner,
                    error_code="EMAIL_JOB_KIND_INVALID",
                    error_class=ErrorClass.NON_RETRYABLE,
                    expected_version=await _current_claim_version(
                        db_session, claimed_job_id, execution_owner
                    ),
                )
            await db_session.commit()
        except JobLeaseLost:
            await db_session.rollback()
            raise
        except Exception as error:
            await db_session.rollback()
            await _recover_failed_email_job(
                claimed_job_id,
                execution_owner,
                kind=claimed_job_kind,
                payload=claimed_job_payload,
                error_code=(
                    "EMAIL_WORKER_TRANSIENT_FAILURE"
                    if _is_retryable_worker_error(error)
                    else "EMAIL_WORKER_NON_RETRYABLE_FAILURE"
                ),
                error_class=(
                    ErrorClass.RETRYABLE
                    if _is_retryable_worker_error(error)
                    else ErrorClass.NON_RETRYABLE
                ),
                max_attempts=(
                    settings.email_classification_max_attempts
                    if claimed_job_kind == EMAIL_CLASSIFY_KIND
                    else (
                        settings.gmail_sync_max_attempts
                        if claimed_job_kind == EMAIL_HISTORY_KIND
                        else None
                    )
                ),
            )
            raise


async def _current_claim_version(
    db_session: AsyncSession, job_id: UUID, execution_owner: str
) -> int:
    version = await db_session.scalar(
        select(JobIntent.version).where(
            JobIntent.id == job_id,
            JobIntent.state == JobState.RUNNING,
            JobIntent.lease_owner == execution_owner,
        )
    )
    if version is None:
        raise JobLeaseLost(job_id)
    return int(version)


async def _finalize_history_sync(
    db_session: AsyncSession, job_id: UUID, payload: dict[str, object]
) -> None:
    connector_id = UUID(str(payload["connector_id"]))
    state = await db_session.scalar(
        select(EmailSyncState)
        .where(EmailSyncState.connector_id == connector_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None or state.active_sync_job_id != job_id:
        return
    state.active_sync_job_id = None
    if state.sync_status is GmailSyncStatus.RECOVERY_TRUNCATED:
        state.version += 1
        return
    if (
        state.pending_history_id is not None
        and state.processed_history_id is not None
        and int(state.pending_history_id) <= int(state.processed_history_id)
    ):
        state.pending_history_id = None
    state.sync_status = GmailSyncStatus.IDLE
    state.last_success_at = datetime.now(UTC)
    logger.info(
        "Gmail synchronization completed",
        extra={
            "organization_id": str(state.organization_id),
            "connector_id": str(connector_id),
            "job_id": str(job_id),
            "trigger": payload.get("trigger"),
            "requested_by": payload.get("requested_by"),
            "outcome": "SUCCEEDED",
        },
    )
    if state.pending_history_id is None or not state.auto_sync_enabled:
        state.version += 1
        return
    state.sync_generation += 1
    state.sync_start_history_id = state.processed_history_id
    next_job = await JobService().enqueue(
        db_session,
        EMAIL_HISTORY_KIND,
        f"email.gmail_history:{connector_id}:generation:{state.sync_generation}",
        {
            **payload,
            "history_id": state.processed_history_id,
            "sync_generation": state.sync_generation,
            "trigger": "PUSH_FOLLOW_UP",
        },
    )
    await OutboxService().add(
        db_session,
        EMAIL_HISTORY_OUTBOX_EVENT_TYPE,
        "job",
        next_job.id,
        {
            "job_id": str(next_job.id),
            "connector_id": str(connector_id),
            "organization_id": str(state.organization_id),
            "sync_generation": state.sync_generation,
        },
    )
    state.active_sync_job_id = next_job.id
    state.sync_status = GmailSyncStatus.RUNNING
    state.version += 1


async def _pause_history_sync(
    db_session: AsyncSession,
    job_id: UUID,
    payload: dict[str, object],
    error_code: str,
) -> None:
    state = await db_session.scalar(
        select(EmailSyncState)
        .where(EmailSyncState.connector_id == UUID(str(payload["connector_id"])))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None or state.active_sync_job_id != job_id:
        return
    state.active_sync_job_id = None
    state.sync_status = GmailSyncStatus.PAUSED_ERROR
    state.last_error_code = error_code
    state.version += 1
    logger.warning(
        "Gmail synchronization paused",
        extra={
            "organization_id": str(state.organization_id),
            "connector_id": str(state.connector_id),
            "job_id": str(job_id),
            "trigger": payload.get("trigger"),
            "requested_by": payload.get("requested_by"),
            "outcome": "PAUSED_ERROR",
            "error_code": error_code,
        },
    )


async def _recover_failed_email_job(
    job_id: UUID,
    execution_owner: str,
    *,
    kind: str,
    payload: dict[str, object],
    error_code: str,
    error_class: ErrorClass,
    max_attempts: int | None = None,
) -> None:
    """Best-effort durable retry that never masks the original business failure."""
    try:
        async with async_sessionmaker() as recovery_session:
            sync_state = None
            if kind == EMAIL_HISTORY_KIND:
                sync_state = await recovery_session.scalar(
                    select(EmailSyncState)
                    .where(
                        EmailSyncState.connector_id
                        == UUID(str(payload["connector_id"]))
                    )
                    .with_for_update()
                )
            current_version = await _current_claim_version(
                recovery_session, job_id, execution_owner
            )
            job = await recovery_session.get(JobIntent, job_id)
            if kind == EMAIL_DRAFT_KIND and job is not None:
                cycle_start = job.cycle_start_attempts or 0
                cycle_limit = job.cycle_attempt_limit or Settings().email_draft_max_attempts
                max_attempts = cycle_start + cycle_limit
            recovered = await JobLeaseService(recovery_session).retry(
                job_id,
                execution_owner,
                error_code=error_code,
                error_class=error_class,
                max_attempts=max_attempts,
                expected_version=current_version,
            )
            if recovered.state is JobState.FAILED and sync_state is not None:
                if sync_state.active_sync_job_id == job_id:
                    sync_state.active_sync_job_id = None
                    sync_state.sync_status = GmailSyncStatus.PAUSED_ERROR
                    sync_state.last_error_code = error_code
                    sync_state.version += 1
            if recovered.state is JobState.FAILED and kind == EMAIL_DRAFT_KIND:
                await _finalize_recovered_draft_failure(
                    recovery_session, job_id, payload, error_code
                )
            await recovery_session.commit()
    except Exception:
        # The caller must retain the original processing exception.  A lost
        # lease means another worker is now authoritative for this JobIntent.
        return


def _is_retryable_worker_error(error: Exception) -> bool:
    if isinstance(error, (GmailTransientError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(error, DisconnectionError):
        return True
    if isinstance(error, (OperationalError, DBAPIError)):
        return bool(getattr(error, "connection_invalidated", False))
    return False


async def _finalize_recovered_draft_failure(
    db_session: AsyncSession,
    job_id: UUID,
    payload: dict[str, object],
    error_code: str,
) -> None:
    item = await db_session.scalar(
        select(EmailWorkItem)
        .where(EmailWorkItem.id == UUID(str(payload["work_item_id"])))
        .with_for_update()
    )
    if item is None or item.active_draft_job_id != job_id:
        return
    before = item.state
    if before is EmailState.DRAFTING:
        item.state = EmailState.DRAFT_RETRY_WAIT
        item.version += 1
        db_session.add(
            EmailStateHistory(
                work_item_id=item.id,
                organization_id=item.organization_id,
                from_state=before,
                to_state=item.state,
                action=EmailAction.DRAFT_FAILED,
                reason_code=error_code,
                actor_id=email_worker_actor_id(item.organization_id),
                actor_type=EMAIL_SYSTEM_ACTOR_TYPE,
                job_id=job_id,
                resource_version=item.version,
            )
        )
    item.active_draft_job_id = None
    item.last_error_code = error_code


async def _consume_delivery_job(
    db_session: AsyncSession, job_id: UUID, settings: Settings
) -> None:
    connector_service = ConnectorService.from_settings(settings)
    gateway_factory = GoogleGmailGatewayFactory.from_settings(settings)
    if connector_service is None or gateway_factory is None:
        raise RuntimeError("Gmail delivery settings are incomplete")
    await EmailDeliveryService(
        db_session,
        worker_id=f"{EMAIL_WORKER_ID}:{uuid4()}",
        connector_service=connector_service,
        gateway_factory=gateway_factory,
    ).send(job_id)


async def _consume_history_with_lease_renewal(
    db_session: AsyncSession,
    job_id: UUID,
    payload: dict[str, object],
    settings: Settings,
    execution_owner: str,
    expected_version: int,
) -> bool:
    operation = asyncio.create_task(_consume_history(db_session, job_id, payload, settings))
    heartbeat = asyncio.create_task(_renew_history_lease(job_id, execution_owner, expected_version))
    done, _pending = await asyncio.wait({operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
    if operation in done:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        return await operation
    operation.cancel()
    await asyncio.gather(operation, return_exceptions=True)
    await heartbeat
    raise JobLeaseLost(job_id)


async def _renew_history_lease(job_id: UUID, execution_owner: str, expected_version: int) -> None:
    interval = EMAIL_LEASE_SECONDS / 3
    while True:
        await asyncio.sleep(interval)
        async with async_sessionmaker() as heartbeat_session:
            renewed = await JobLeaseService(heartbeat_session).renew(
                job_id,
                execution_owner,
                EMAIL_LEASE_SECONDS,
                expected_version=expected_version,
            )
            await heartbeat_session.commit()
            expected_version = renewed.version


async def _consume_history(
    db_session: AsyncSession, job_id: UUID, payload: dict[str, object], settings: Settings
) -> bool:
    connector_service = ConnectorService.from_settings(settings)
    gateway_factory = GoogleGmailGatewayFactory.from_settings(settings)
    if connector_service is None or gateway_factory is None:
        raise RuntimeError("Gmail connector settings are incomplete")
    service = EmailIngestionService(
        db_session,
        classifier=_build_classifier(settings),
        connector_service=connector_service,
        gateway_factory=gateway_factory,
        draft_max_attempts=settings.email_draft_max_attempts,
        recovery_lookback_days=settings.gmail_recovery_lookback_days,
        recovery_max_messages=settings.gmail_recovery_max_messages,
    )
    result = await service.ingest_history(
        UUID(str(payload["connector_id"])),
        UUID(str(payload["knowledge_base_id"])),
        commit=False,
        job_id=job_id,
    )
    return result.reauth_required


async def _consume_classification(
    db_session: AsyncSession,
    job_id: UUID,
    payload: dict[str, object],
    settings: Settings,
) -> None:
    work_item_id = UUID(str(payload["work_item_id"]))
    canonical_job_id = await db_session.scalar(
        select(JobIntent.id)
        .where(
            JobIntent.kind == EMAIL_CLASSIFY_KIND,
            JobIntent.payload["work_item_id"].astext == str(work_item_id),
            JobIntent.state != JobState.SUCCEEDED,
        )
        .order_by(JobIntent.created_at, JobIntent.id)
        .limit(1)
    )
    if canonical_job_id != job_id:
        raise EmailClassificationFailure(
            "EMAIL_CLASSIFICATION_SUPERSEDED", ErrorClass.NON_RETRYABLE
        )
    await EmailIngestionService(
        db_session,
        classifier=_build_classifier(settings),
        classification_retry_after_cap_seconds=settings.email_classification_retry_max_seconds,
    ).process_classification(work_item_id, job_id=job_id)


async def _consume_draft(
    db_session: AsyncSession,
    job_id: UUID,
    payload: dict[str, object],
    settings: Settings,
) -> None:
    item_id = UUID(str(payload["work_item_id"]))
    item = await db_session.get(EmailWorkItem, item_id)
    if item is None:
        raise LookupError("email work item not found")
    if item.active_draft_job_id != job_id:
        raise EmailDraftFailure(
            "EMAIL_DRAFT_SUPERSEDED", ErrorClass.NON_RETRYABLE
        )
    principal = email_worker_principal(item.organization_id, item.knowledge_base_id, job_id)
    grounded = GroundedAnswerService.from_settings(settings, session_factory=async_sessionmaker)
    reviewer_instruction = payload.get("reviewer_instruction")
    await EmailDraftingService(db_session, grounded, principal).generate(
        item.id,
        job_id=job_id,
        reviewer_instruction=(
            reviewer_instruction if isinstance(reviewer_instruction, str) else None
        ),
    )


def _build_classifier(settings: Settings) -> EmailClassifier:
    if settings.openai_api_key is None:
        return _UnavailableEmailClassifier()
    return OpenAIEmailClassifier.from_settings(settings)
