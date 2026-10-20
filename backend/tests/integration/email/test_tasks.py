import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.email.classification import ClassificationExecution, EmailClassificationFailure
from app.modules.email.gmail_gateway import (
    GmailHistoryPage,
    GmailMessage,
    GmailMessagePage,
    GmailProfile,
)
from app.modules.email.ingestion import EmailIngestionService
from app.modules.email.models import (
    EmailCategory,
    EmailPriority,
    EmailState,
    EmailSyncState,
    EmailWorkItem,
    GmailSyncStatus,
)
from app.modules.email.push import GmailPushService, VerifiedGmailNotification
from app.modules.email.schemas import EmailClassification
from app.modules.email.tasks import (
    _consume_email_job,
    _dispatch_email_history_outbox_event,
    _enqueue_gmail_history_jobs,
)
from app.modules.identity.models import Organization
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobService
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent


@pytest.mark.asyncio
async def test_each_completed_reconciliation_gets_a_new_durable_job_and_outbox(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    for expected_count in (1, 2, 3):
        await _enqueue_gmail_history_jobs(db_session=db_session)
        jobs = list(
            (
                await db_session.scalars(
                    select(JobIntent)
                    .where(
                        JobIntent.kind == "email.gmail_history",
                        JobIntent.idempotency_key.like(f"email.gmail_history:{connector.id}:%"),
                    )
                    .order_by(JobIntent.created_at, JobIntent.id)
                )
            ).all()
        )
        assert len(jobs) == expected_count
        for dispatched_job in jobs:
            dispatched_job.state = JobState.SUCCEEDED
            dispatched_job.version = 3
        await db_session.commit()

    assert len(set(job.idempotency_key for job in jobs)) == 3
    assert (
        await db_session.scalar(
                select(func.count(OutboxEvent.event_id)).where(
                    OutboxEvent.event_type == "email.gmail_history.requested",
                    OutboxEvent.aggregate_id.in_([job.id for job in jobs]),
                )
        )
        == 3
    )


@pytest.mark.asyncio
async def test_history_job_key_is_bounded_for_provider_page_tokens(
    db_session: AsyncSession, email_context: dict[str, object], monkeypatch
) -> None:
    connector = email_context["connector"]
    db_session.add(
        EmailSyncState(
            organization_id=connector.organization_id,
            connector_id=connector.id,
            processed_history_id="cursor-101",
            pending_page_token=f"history:{'x' * 900}",
            auto_sync_enabled=True,
        )
    )
    await db_session.commit()
    monkeypatch.setattr("app.modules.email.tasks.email_job.delay", lambda _job_id: None)

    await _enqueue_gmail_history_jobs(db_session=db_session)

    job = await db_session.scalar(
        select(JobIntent).where(
            JobIntent.kind == "email.gmail_history",
            JobIntent.idempotency_key.like(f"email.gmail_history:{connector.id}:%"),
        )
    )
    assert job is not None
    assert len(job.idempotency_key) <= 255


@pytest.mark.asyncio
async def test_scheduled_history_sync_only_creates_one_job_when_due(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    now = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    state = EmailSyncState(
        organization_id=connector.organization_id,
        connector_id=connector.id,
        processed_history_id="cursor-100",
        auto_sync_enabled=False,
        last_success_at=now,
    )
    db_session.add(state)
    await db_session.commit()

    await _enqueue_gmail_history_jobs(
        db_session=db_session,
        now=now + timedelta(minutes=59),
        interval_seconds=3600,
    )
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "email.gmail_history",
            JobIntent.payload["connector_id"].astext == str(connector.id),
        )
    ) == 0

    await _enqueue_gmail_history_jobs(
        db_session=db_session,
        now=now + timedelta(hours=1),
        interval_seconds=3600,
    )
    await _enqueue_gmail_history_jobs(
        db_session=db_session,
        now=now + timedelta(hours=2),
        interval_seconds=3600,
    )

    jobs = list(
        (
            await db_session.scalars(
                select(JobIntent).where(
                    JobIntent.kind == "email.gmail_history",
                    JobIntent.payload["connector_id"].astext == str(connector.id),
                )
            )
        ).all()
    )
    await db_session.refresh(state)
    assert len(jobs) == 1
    assert jobs[0].payload["trigger"] == "SCHEDULED"
    assert state.active_sync_job_id == jobs[0].id
    assert state.sync_status is GmailSyncStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sync_status",
    [
        GmailSyncStatus.PAUSED_ERROR,
        GmailSyncStatus.RECOVERY_REQUIRED,
        GmailSyncStatus.RECOVERY_TRUNCATED,
    ],
)
async def test_scheduled_history_sync_does_not_reactivate_paused_states(
    db_session: AsyncSession,
    email_context: dict[str, object],
    sync_status: GmailSyncStatus,
) -> None:
    connector = email_context["connector"]
    db_session.add(
        EmailSyncState(
            organization_id=connector.organization_id,
            connector_id=connector.id,
            processed_history_id="cursor-100",
            sync_status=sync_status,
        )
    )
    await db_session.commit()

    await _enqueue_gmail_history_jobs(
        db_session=db_session,
        now=datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
        interval_seconds=3600,
    )

    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind == "email.gmail_history",
            JobIntent.payload["connector_id"].astext == str(connector.id),
        )
    ) == 0


class BlockingClassifier:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def classify(self, _subject: str, _body: str) -> ClassificationExecution:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return ClassificationExecution(
            EmailClassification(
                category=EmailCategory.INFORMATIONAL,
                priority=EmailPriority.NORMAL,
                reply_required=False,
            ),
            "paid-fixture",
            "fixture-v1",
            1,
            2,
            1,
            0.0,
        )


class SequenceClassifier:
    def __init__(self, results: list[ClassificationExecution | BaseException]) -> None:
        self.results = results
        self.calls = 0

    async def classify(self, _subject: str, _body: str) -> ClassificationExecution:
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, BaseException):
            raise result
        return result


def _informational_execution() -> ClassificationExecution:
    return ClassificationExecution(
        EmailClassification(
            category=EmailCategory.INFORMATIONAL,
            priority=EmailPriority.NORMAL,
            reply_required=False,
        ),
        "fixture",
        "fixture-v1",
        1,
        2,
        1,
        0.0,
    )


def _action_required_execution() -> ClassificationExecution:
    return ClassificationExecution(
        EmailClassification(
            category=EmailCategory.ACTION_REQUIRED,
            priority=EmailPriority.HIGH,
            reply_required=True,
        ),
        "fixture",
        "fixture-v1",
        1,
        2,
        1,
        0.0,
    )


async def _classification_job() -> tuple[UUID, UUID]:
    async with async_sessionmaker() as setup:
        organization = Organization(name=f"Classification {uuid4()}")
        setup.add(organization)
        await setup.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id, public_key=f"classify-{uuid4().hex}"
        )
        secret = ConnectorSecret(
            organization_id=organization.id,
            ciphertext=b"fixture",
            encrypted_data_key=b"fixture",
            nonce=b"fixture",
            algorithm="AES-256-GCM",
            key_version="fixture",
        )
        setup.add_all([knowledge_base, secret])
        await setup.flush()
        connector = Connector(
            organization_id=organization.id,
            kind=ConnectorKind.GMAIL,
            status=ConnectorStatus.ACTIVE,
            secret_id=secret.id,
        )
        setup.add(connector)
        await setup.flush()
        message = GmailMessage(
            id=f"gmail-{uuid4().hex}",
            thread_id="thread-classification",
            history_id="cursor-classification",
            sender="customer@example.test",
            recipients=("support@example.test",),
            subject="Classification fixture",
            body="Please classify this message.",
            received_at=datetime.now(UTC),
            raw_content_ref="gmail://fixture/classification",
        )
        item = await EmailIngestionService(
            setup, classifier=SequenceClassifier([])
        ).ingest_message(
            message,
            organization_id=organization.id,
            connector_id=connector.id,
            knowledge_base_id=knowledge_base.id,
        )
        job = await setup.scalar(
            select(JobIntent).where(
                JobIntent.kind == "email.classify",
                JobIntent.payload["work_item_id"].astext == str(item.id),
            )
        )
        assert job is not None
        item_id = item.id
        job_id = job.id
        await setup.commit()
        return item_id, job_id


@pytest.mark.asyncio
async def test_auth_failure_terminally_fails_original_classification_job(
    monkeypatch,
) -> None:
    item_id, job_id = await _classification_job()
    classifier = SequenceClassifier(
        [
            EmailClassificationFailure(
                "EMAIL_CLASSIFICATION_AUTHENTICATION_FAILED",
                ErrorClass.NON_RETRYABLE,
            )
        ]
    )
    monkeypatch.setattr("app.modules.email.tasks._build_classifier", lambda _settings: classifier)

    await _consume_email_job(job_id)

    async with async_sessionmaker() as verify:
        persisted = await verify.get(JobIntent, job_id)
        persisted_item = await verify.get(EmailWorkItem, item_id)
        assert persisted is not None
        assert persisted_item is not None
        assert persisted.state is JobState.FAILED
        assert persisted.attempts == 1
        assert persisted.last_error_code == "EMAIL_CLASSIFICATION_AUTHENTICATION_FAILED"
        assert persisted_item.state is EmailState.DRAFT_RETRY_WAIT
        assert persisted_item.last_error_code == persisted.last_error_code
        assert classifier.calls == 1
        assert (
            await verify.scalar(
                select(func.count(JobIntent.id)).where(
                    JobIntent.kind == "email.classify",
                    JobIntent.payload["work_item_id"].astext == str(item_id),
                )
            )
            == 1
        )

    await _consume_email_job(job_id)
    assert classifier.calls == 1


@pytest.mark.asyncio
async def test_transient_failure_retries_same_job_then_succeeds(
    monkeypatch,
) -> None:
    item_id, job_id = await _classification_job()
    classifier = SequenceClassifier(
        [
            EmailClassificationFailure(
                "EMAIL_CLASSIFICATION_TIMEOUT", ErrorClass.RETRYABLE
            ),
            _action_required_execution(),
        ]
    )
    monkeypatch.setattr("app.modules.email.tasks._build_classifier", lambda _settings: classifier)

    await _consume_email_job(job_id)
    async with async_sessionmaker() as advance:
        first_attempt = await advance.get(JobIntent, job_id)
        assert first_attempt is not None
        assert first_attempt.state is JobState.PENDING
        assert first_attempt.next_attempt_at is not None
        first_attempt.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await advance.commit()

    await _consume_email_job(job_id)

    async with async_sessionmaker() as verify:
        persisted = await verify.get(JobIntent, job_id)
        persisted_item = await verify.get(EmailWorkItem, item_id)
        assert persisted is not None
        assert persisted_item is not None
        assert persisted.id == job_id
        assert persisted.state is JobState.SUCCEEDED
        assert persisted.attempts == 2
        assert persisted_item.state is EmailState.DRAFTING
        assert persisted_item.category is EmailCategory.ACTION_REQUIRED
        assert classifier.calls == 2
        assert (
            await verify.scalar(
                select(func.count(JobIntent.id)).where(
                    JobIntent.kind == "email.draft",
                    JobIntent.payload["work_item_id"].astext == str(item_id),
                )
            )
            == 1
        )

    await _consume_email_job(job_id)
    assert classifier.calls == 2


@pytest.mark.asyncio
async def test_retryable_classification_stops_at_attempt_limit(
    monkeypatch,
) -> None:
    item_id, job_id = await _classification_job()
    classifier = SequenceClassifier(
        [
            EmailClassificationFailure(
                "EMAIL_CLASSIFICATION_TIMEOUT", ErrorClass.RETRYABLE
            ),
            EmailClassificationFailure(
                "EMAIL_CLASSIFICATION_TIMEOUT", ErrorClass.RETRYABLE
            ),
        ]
    )
    monkeypatch.setenv("EMAIL_CLASSIFICATION_MAX_ATTEMPTS", "2")
    monkeypatch.setattr("app.modules.email.tasks._build_classifier", lambda _settings: classifier)

    await _consume_email_job(job_id)
    async with async_sessionmaker() as advance:
        pending = await advance.get(JobIntent, job_id)
        assert pending is not None
        pending.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await advance.commit()
    await _consume_email_job(job_id)

    async with async_sessionmaker() as verify:
        stopped = await verify.get(JobIntent, job_id)
        assert stopped is not None
        assert stopped.state is JobState.FAILED
        assert stopped.attempts == 2
        assert stopped.next_attempt_at is None
        assert classifier.calls == 2
        assert (
            await verify.scalar(
                select(func.count(JobIntent.id)).where(
                    JobIntent.kind == "email.classify",
                    JobIntent.payload["work_item_id"].astext == str(item_id),
                )
            )
            == 1
        )

    await _consume_email_job(job_id)
    assert classifier.calls == 2


@pytest.mark.asyncio
async def test_historical_duplicate_classification_job_is_superseded_without_model_call(
    monkeypatch,
) -> None:
    item_id, original_id = await _classification_job()
    async with async_sessionmaker() as setup:
        original = await setup.get(JobIntent, original_id)
        assert original is not None
        duplicate = await JobService().enqueue(
            setup,
            "email.classify",
            f"historical-duplicate:{uuid4()}",
            dict(original.payload),
        )
        duplicate_id = duplicate.id
        await setup.commit()
    classifier = SequenceClassifier([_informational_execution()])
    monkeypatch.setattr("app.modules.email.tasks._build_classifier", lambda _settings: classifier)

    await _consume_email_job(duplicate_id)

    async with async_sessionmaker() as verify:
        persisted = await verify.get(JobIntent, duplicate_id)
        assert persisted is not None
        assert persisted.state is JobState.FAILED
        assert persisted.last_error_code == "EMAIL_CLASSIFICATION_SUPERSEDED"
        assert classifier.calls == 0
        original_job = await verify.get(JobIntent, original_id)
        persisted_item = await verify.get(EmailWorkItem, item_id)
        assert original_job is not None and original_job.state is JobState.PENDING
        assert persisted_item is not None and persisted_item.category is None


@pytest.mark.asyncio
async def test_classification_lease_loss_rolls_back_result_and_downstream_job(monkeypatch) -> None:
    item_id, job_id = await _classification_job()
    classifier = SequenceClassifier([_action_required_execution()])
    monkeypatch.setattr("app.modules.email.tasks._build_classifier", lambda _settings: classifier)

    async def lose_lease(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise JobLeaseLost(job_id)

    monkeypatch.setattr("app.modules.email.tasks.JobLeaseService.complete", lose_lease)

    with pytest.raises(JobLeaseLost):
        await _consume_email_job(job_id)

    async with async_sessionmaker() as verify:
        persisted_item = await verify.get(EmailWorkItem, item_id)
        persisted_job = await verify.get(JobIntent, job_id)
        assert persisted_item is not None
        assert persisted_job is not None
        assert persisted_item.state is EmailState.INGESTED
        assert persisted_item.category is None
        assert persisted_job.state is JobState.RUNNING
        assert (
            await verify.scalar(
                select(func.count(JobIntent.id)).where(
                    JobIntent.kind == "email.draft",
                    JobIntent.payload["work_item_id"].astext == str(item_id),
                )
            )
            == 0
        )


class OnePageGateway:
    def __init__(self, message: GmailMessage) -> None:
        self.message = message

    async def get_profile(self) -> GmailProfile:
        return GmailProfile("support@example.test", "101")

    async def list_recent_inbox(
        self, *, after, page_token: str | None, max_results: int  # type: ignore[no-untyped-def]
    ) -> GmailMessagePage:
        del after, page_token, max_results
        return GmailMessagePage((self.message.id,), None)

    async def list_history(
        self, _history_id: str | None, _page_token: str | None = None
    ) -> GmailHistoryPage:
        return GmailHistoryPage((self.message.id,), "120", None)

    async def get_message(self, _message_id: str) -> GmailMessage:
        return self.message


@pytest.mark.asyncio
async def test_history_consumer_enqueues_classification_without_paid_model_call(
    monkeypatch,
) -> None:
    classifier = BlockingClassifier()
    organization_id = uuid4()
    mailbox_address = f"push-chain-{uuid4().hex}@example.test"
    async with async_sessionmaker() as setup:
        organization = Organization(id=organization_id, name=f"Lease owner {uuid4()}")
        setup.add(organization)
        await setup.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id, public_key=f"lease-{uuid4().hex}"
        )
        secret = ConnectorSecret(
            organization_id=organization.id,
            ciphertext=b"fixture",
            encrypted_data_key=b"fixture",
            nonce=b"fixture",
            algorithm="AES-256-GCM",
            key_version="fixture",
        )
        setup.add_all([knowledge_base, secret])
        await setup.flush()
        connector = Connector(
            organization_id=organization.id,
            kind=ConnectorKind.GMAIL,
            status=ConnectorStatus.ACTIVE,
            secret_id=secret.id,
        )
        setup.add(connector)
        await setup.flush()
        setup.add(
            EmailSyncState(
                organization_id=organization.id,
                connector_id=connector.id,
                mailbox_address=mailbox_address,
                processed_history_id="100",
                auto_sync_enabled=True,
            )
        )
        await setup.flush()
        job_id = await GmailPushService(
            setup, receipt_retention_days=14
        ).accept(
            VerifiedGmailNotification(
                subscription_name="projects/p/subscriptions/gmail",
                pubsub_message_id=f"chain-{uuid4()}",
                published_at=datetime.now(UTC),
                mailbox_address=mailbox_address,
                history_id="120",
            )
        )
        assert job_id is not None
        connector_id = connector.id
        knowledge_base_id = knowledge_base.id
        event_id = await setup.scalar(
            select(OutboxEvent.event_id).where(OutboxEvent.aggregate_id == job_id)
        )
        assert event_id is not None

    published: list[str] = []
    monkeypatch.setattr(
        "app.modules.email.tasks.email_job.delay",
        lambda value: published.append(value),
    )
    assert await _dispatch_email_history_outbox_event(event_id)
    assert published == [str(job_id)]

    message = GmailMessage(
        id=f"gmail-{uuid4().hex}",
        thread_id="thread-renewal",
        history_id="120",
        sender="customer@example.test",
        recipients=("support@example.test",),
        subject="Slow classification",
        body="A page can take longer than one lease.",
        received_at=datetime.now(UTC),
        raw_content_ref="gmail://fixture/renewal",
    )

    async def consume_history(db_session, running_job_id, payload, _settings):  # type: ignore[no-untyped-def]
        result = await EmailIngestionService(
            db_session,
            gateway=OnePageGateway(message),
            classifier=classifier,
        ).ingest_history(
            connector_id,
            knowledge_base_id,
            commit=False,
            job_id=running_job_id,
        )
        return result.reauth_required

    monkeypatch.setattr("app.modules.email.tasks._consume_history", consume_history)

    await _consume_email_job(job_id)

    async with async_sessionmaker() as verify:
        persisted_job = await verify.get(JobIntent, job_id)
        sync_state = await verify.scalar(
            select(EmailSyncState).where(EmailSyncState.connector_id == connector_id)
        )
        assert persisted_job is not None
        assert persisted_job.state is JobState.SUCCEEDED
        assert classifier.calls == 0
        assert (
            await verify.scalar(
                select(func.count(EmailWorkItem.id)).where(
                    EmailWorkItem.organization_id == organization_id
                )
            )
            == 1
        )
        assert sync_state is not None
        assert sync_state.processed_history_id == "120"
        assert sync_state.pending_history_id is None
        assert sync_state.active_sync_job_id is None
        assert (
            await verify.scalar(
                select(func.count(JobIntent.id)).where(
                    JobIntent.kind == "email.classify",
                    JobIntent.payload["organization_id"].astext == str(organization_id),
                )
            )
            == 1
        )
