import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.email.classification import ClassificationExecution
from app.modules.email.gmail_gateway import GmailHistoryPage, GmailMessage
from app.modules.email.ingestion import EmailIngestionService
from app.modules.email.models import EmailCategory, EmailPriority, EmailSyncState, EmailWorkItem
from app.modules.email.schemas import EmailClassification
from app.modules.email.tasks import _consume_email_job, _enqueue_gmail_history_jobs
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobService
from app.modules.knowledge.models import KnowledgeBase


@pytest.mark.asyncio
async def test_each_completed_history_poll_gets_a_new_durable_job_key(
    db_session: AsyncSession, email_context: dict[str, object], monkeypatch
) -> None:
    connector = email_context["connector"]
    dispatched: list[UUID] = []
    monkeypatch.setattr(
        "app.modules.email.tasks.email_job.delay",
        lambda job_id: dispatched.append(UUID(job_id)),
    )

    for expected_count in (1, 2, 3):
        dispatched_before = set(dispatched)
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
        newly_dispatched = set(dispatched) - dispatched_before
        dispatched_job = next(job for job in jobs if job.id in newly_dispatched)
        dispatched_job.state = JobState.SUCCEEDED
        dispatched_job.version = 3
        await db_session.commit()

    assert len(set(job.idempotency_key for job in jobs)) == 3
    assert {job.id for job in jobs}.issubset(dispatched)


@pytest.mark.asyncio
async def test_history_job_key_is_bounded_for_provider_page_tokens(
    db_session: AsyncSession, email_context: dict[str, object], monkeypatch
) -> None:
    connector = email_context["connector"]
    db_session.add(
        EmailSyncState(
            organization_id=connector.organization_id,
            connector_id=connector.id,
            history_id="cursor-101",
            pending_page_token=f"history:{'x' * 900}",
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


class OnePageGateway:
    def __init__(self, message: GmailMessage) -> None:
        self.message = message

    async def list_history(
        self, _history_id: str | None, _page_token: str | None = None
    ) -> GmailHistoryPage:
        return GmailHistoryPage((self.message.id,), "cursor-2", None)

    async def get_message(self, _message_id: str) -> GmailMessage:
        return self.message


@pytest.mark.asyncio
async def test_history_consumer_renews_real_pg_lease_without_duplicate_paid_work(
    monkeypatch,
) -> None:
    classifier = BlockingClassifier()
    organization_id = uuid4()
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
        job = await JobService().enqueue(
            setup,
            "email.gmail_history",
            f"renewal-{uuid4()}",
            {
                "connector_id": str(connector.id),
                "organization_id": str(organization.id),
                "knowledge_base_id": str(knowledge_base.id),
                "history_id": None,
                "page_token": None,
            },
        )
        job_id = job.id
        connector_id = connector.id
        knowledge_base_id = knowledge_base.id
        await setup.commit()

    message = GmailMessage(
        id=f"gmail-{uuid4().hex}",
        thread_id="thread-renewal",
        history_id="cursor-2",
        sender="customer@example.test",
        recipients=("support@example.test",),
        subject="Slow classification",
        body="A page can take longer than one lease.",
        received_at=datetime.now(UTC),
        raw_content_ref="gmail://fixture/renewal",
    )

    async def consume_history(db_session, running_job, _settings):  # type: ignore[no-untyped-def]
        result = await EmailIngestionService(
            db_session,
            gateway=OnePageGateway(message),
            classifier=classifier,
        ).ingest_history(
            connector_id,
            knowledge_base_id,
            commit=False,
            job_id=running_job.id,
        )
        return result.reauth_required

    monkeypatch.setattr("app.modules.email.tasks.EMAIL_LEASE_SECONDS", 1)
    monkeypatch.setattr("app.modules.email.tasks._consume_history", consume_history)

    first = asyncio.create_task(_consume_email_job(job_id))
    await asyncio.wait_for(classifier.started.wait(), timeout=2)
    await asyncio.sleep(1.2)
    await _consume_email_job(job_id)
    classifier.release.set()
    await asyncio.wait_for(first, timeout=3)

    async with async_sessionmaker() as verify:
        persisted_job = await verify.get(JobIntent, job_id)
        sync_state = await verify.scalar(
            select(EmailSyncState).where(EmailSyncState.connector_id == connector_id)
        )
        assert persisted_job is not None
        assert persisted_job.state is JobState.SUCCEEDED
        assert classifier.calls == 1
        assert (
            await verify.scalar(
                select(func.count(EmailWorkItem.id)).where(
                    EmailWorkItem.organization_id == organization_id
                )
            )
            == 1
        )
        assert sync_state is not None
        assert sync_state.history_id == "cursor-2"
