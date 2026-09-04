"""Celery consumers for durable Gmail history, classification, and draft intents."""

import asyncio
from hashlib import sha256
from uuid import UUID, uuid4

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import async_sessionmaker
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.email.actors import email_worker_principal
from app.modules.email.classification import (
    AnthropicEmailClassifier,
    EmailClassifier,
    EmailClassifierUnavailable,
)
from app.modules.email.delivery import EMAIL_DELIVERY_KIND, EmailDeliveryService
from app.modules.email.drafting import EmailDraftingService
from app.modules.email.gmail_gateway import GoogleGmailGatewayFactory
from app.modules.email.ingestion import EmailIngestionService
from app.modules.email.models import DeliveryIntent, EmailSyncState, EmailWorkItem
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent, ProcessedEvent
from app.modules.outbox.service import OutboxService
from app.modules.rag.answer_service import GroundedAnswerService

EMAIL_HISTORY_KIND = "email.gmail_history"
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


class _UnavailableEmailClassifier:
    async def classify(self, _subject: str, _body: str):  # type: ignore[no-untyped-def]
        raise EmailClassifierUnavailable("ANTHROPIC_API_KEY is not configured")


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


@shared_task(name="app.modules.email.tasks.gmail_history_poll")  # type: ignore[untyped-decorator]
def gmail_history_poll() -> None:
    asyncio.run(_enqueue_gmail_history_jobs())


@shared_task(name="app.modules.email.tasks.dispatch_pending_email_jobs")  # type: ignore[untyped-decorator]
def dispatch_pending_email_jobs() -> None:
    asyncio.run(_dispatch_pending_email_jobs())


async def _enqueue_gmail_history_jobs(*, db_session: AsyncSession | None = None) -> None:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _enqueue_gmail_history_jobs(db_session=owned_session)
        return
    rows = (
        await db_session.execute(
            select(Connector, KnowledgeBase.id, EmailSyncState)
            .join(
                KnowledgeBase,
                KnowledgeBase.organization_id == Connector.organization_id,
            )
            .outerjoin(EmailSyncState, EmailSyncState.connector_id == Connector.id)
            .where(
                Connector.kind == ConnectorKind.GMAIL,
                Connector.status == ConnectorStatus.ACTIVE,
            )
        )
    ).all()
    job_ids: list[UUID] = []
    for connector, knowledge_base_id, sync_state in rows:
        cursor = sync_state.history_id if sync_state is not None else None
        page_token = sync_state.pending_page_token if sync_state is not None else None
        fingerprint = sha256(f"{cursor or 'initial'}\0{page_token or 'first'}".encode()).hexdigest()
        base_key = f"email.gmail_history:{connector.id}:{fingerprint}"
        matching_jobs = list(
            (
                await db_session.scalars(
                    select(JobIntent).where(
                        JobIntent.kind == EMAIL_HISTORY_KIND,
                        or_(
                            JobIntent.idempotency_key == base_key,
                            JobIntent.idempotency_key.like(f"{base_key}:after:%"),
                        ),
                    )
                )
            ).all()
        )
        referenced_predecessors = {
            predecessor
            for candidate in matching_jobs
            if (predecessor := _history_job_predecessor(candidate.idempotency_key, base_key))
            is not None
        }
        existing = next(
            (
                candidate
                for candidate in matching_jobs
                if candidate.id not in referenced_predecessors
            ),
            None,
        )
        if existing is not None and existing.state in {
            JobState.PENDING,
            JobState.RUNNING,
            JobState.RECONCILIATION,
        }:
            job_ids.append(existing.id)
            continue
        key = base_key if existing is None else f"{base_key}:after:{existing.id}:{existing.version}"
        job = await JobService().enqueue(
            db_session,
            EMAIL_HISTORY_KIND,
            key,
            {
                "connector_id": str(connector.id),
                "organization_id": str(connector.organization_id),
                "knowledge_base_id": str(knowledge_base_id),
                "history_id": cursor,
                "page_token": page_token,
            },
        )
        await OutboxService().add(
            db_session,
            "email.gmail_history.requested",
            "job",
            job.id,
            {
                "connector_id": str(connector.id),
                "organization_id": str(connector.organization_id),
            },
        )
        job_ids.append(job.id)
    await db_session.commit()
    for job_id in job_ids:
        email_job.delay(str(job_id))


def _history_job_predecessor(idempotency_key: str, base_key: str) -> UUID | None:
    prefix = f"{base_key}:after:"
    if not idempotency_key.startswith(prefix):
        return None
    raw_id, separator, _version = idempotency_key.removeprefix(prefix).partition(":")
    if not separator:
        return None
    try:
        return UUID(raw_id)
    except ValueError:
        return None


async def _dispatch_pending_email_jobs() -> None:
    async with async_sessionmaker() as db_session:
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
        lease_service = JobLeaseService(db_session)
        execution_owner = f"{EMAIL_WORKER_ID}:{uuid4()}"
        job = await lease_service.claim(job_id, execution_owner, lease_seconds=EMAIL_LEASE_SECONDS)
        if job is None:
            return
        await db_session.commit()
        try:
            if job.kind == EMAIL_HISTORY_KIND:
                result = await _consume_history_with_lease_renewal(
                    db_session, job, settings, execution_owner
                )
                if result:
                    await lease_service.retry(
                        job.id,
                        execution_owner,
                        error_code="GMAIL_REAUTH_REQUIRED",
                        error_class=ErrorClass.NON_RETRYABLE,
                        expected_version=job.version,
                    )
                else:
                    await lease_service.complete(
                        job.id, execution_owner, expected_version=job.version
                    )
            elif job.kind == EMAIL_CLASSIFY_KIND:
                await _consume_classification(db_session, job, settings)
                await lease_service.complete(job.id, execution_owner, expected_version=job.version)
            elif job.kind == EMAIL_DRAFT_KIND:
                await _consume_draft(db_session, job, settings)
                await lease_service.complete(job.id, execution_owner, expected_version=job.version)
            else:
                await lease_service.retry(
                    job.id,
                    execution_owner,
                    error_code="EMAIL_JOB_KIND_INVALID",
                    error_class=ErrorClass.NON_RETRYABLE,
                    expected_version=job.version,
                )
            await db_session.commit()
        except JobLeaseLost:
            await db_session.rollback()
            raise
        except Exception:
            await db_session.rollback()
            await lease_service.retry(
                job.id,
                execution_owner,
                error_code="EMAIL_WORKER_TRANSIENT_FAILURE",
                error_class=ErrorClass.RETRYABLE,
                expected_version=job.version,
            )
            await db_session.commit()
            raise


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
    job: JobIntent,
    settings: Settings,
    execution_owner: str,
) -> bool:
    operation = asyncio.create_task(_consume_history(db_session, job, settings))
    heartbeat = asyncio.create_task(_renew_history_lease(job.id, execution_owner, job.version))
    done, _pending = await asyncio.wait({operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
    if operation in done:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        return await operation
    operation.cancel()
    await asyncio.gather(operation, return_exceptions=True)
    await heartbeat
    raise JobLeaseLost(job.id)


async def _renew_history_lease(job_id: UUID, execution_owner: str, expected_version: int) -> None:
    interval = EMAIL_LEASE_SECONDS / 3
    while True:
        await asyncio.sleep(interval)
        async with async_sessionmaker() as heartbeat_session:
            await JobLeaseService(heartbeat_session).renew(
                job_id,
                execution_owner,
                EMAIL_LEASE_SECONDS,
                expected_version=expected_version,
            )
            await heartbeat_session.commit()


async def _consume_history(
    db_session: AsyncSession, job: JobIntent, settings: Settings
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
    )
    result = await service.ingest_history(
        UUID(str(job.payload["connector_id"])),
        UUID(str(job.payload["knowledge_base_id"])),
        commit=False,
        job_id=job.id,
    )
    return result.reauth_required


async def _consume_classification(
    db_session: AsyncSession,
    job: JobIntent,
    settings: Settings,
) -> None:
    await EmailIngestionService(
        db_session, classifier=_build_classifier(settings)
    ).process_classification(UUID(str(job.payload["work_item_id"])), job_id=job.id)


async def _consume_draft(
    db_session: AsyncSession,
    job: JobIntent,
    settings: Settings,
) -> None:
    item_id = UUID(str(job.payload["work_item_id"]))
    item = await db_session.get(EmailWorkItem, item_id)
    if item is None:
        raise LookupError("email work item not found")
    principal = email_worker_principal(item.organization_id, item.knowledge_base_id, job.id)
    grounded = GroundedAnswerService.from_settings(settings)
    await EmailDraftingService(db_session, grounded, principal).generate(item.id, job_id=job.id)


def _build_classifier(settings: Settings) -> EmailClassifier:
    if settings.anthropic_api_key is None:
        return _UnavailableEmailClassifier()
    return AnthropicEmailClassifier(
        settings.anthropic_api_key.get_secret_value(), model=settings.anthropic_model
    )
