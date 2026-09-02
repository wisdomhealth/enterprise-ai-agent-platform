import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery import create_celery
from app.core.database import async_sessionmaker, engine
from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.email.delivery import EmailDeliveryService, ReconciliationRequired
from app.modules.email.gmail_gateway import GmailConnection
from app.modules.email.models import (
    DeliveryAttempt,
    DeliveryIntent,
    EmailDraftVersion,
    EmailState,
    EmailWorkItem,
    SuccessfulDelivery,
)
from app.modules.email.reconciliation import ReconciliationService
from app.modules.email.review import EmailReviewService
from app.modules.email.tasks import (
    _consume_email_delivery_outbox_event,
    _consume_email_job,
    _dispatch_email_delivery_outbox_event,
    _dispatch_pending_email_delivery_outbox_events,
    _dispatch_pending_email_jobs,
    dispatch_pending_email_delivery_outbox_events,
    email_delivery_outbox_event,
)
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent, ProcessedEvent


async def _approve(db_session: AsyncSession, context: dict[str, object]) -> DeliveryIntent:
    item = context["item"]
    draft = context["draft"]
    result = await EmailReviewService(db_session, context["principal"]).approve(
        item.id,
        expected_version=item.version,
        current_draft_id=draft.id,
    )
    assert result.state is EmailState.SEND_PENDING
    intent = await db_session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.work_item_id == item.id)
    )
    assert intent is not None
    return intent


async def _seed_committed_delivery_intent() -> tuple[UUID, UUID, UUID, Principal]:
    """Create a delivery intent visible to independent worker sessions."""

    async with async_sessionmaker() as session:
        organization = Organization(name=f"Concurrent delivery {uuid4()}")
        session.add(organization)
        await session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id,
            public_key=f"delivery-{uuid4().hex}",
        )
        secret = ConnectorSecret(
            organization_id=organization.id,
            ciphertext=b"fixture-ciphertext",
            encrypted_data_key=b"fixture-key",
            nonce=b"fixture-nonce",
            algorithm="AES-256-GCM",
            key_version="fixture",
        )
        session.add_all((knowledge_base, secret))
        await session.flush()
        connector = Connector(
            organization_id=organization.id,
            kind=ConnectorKind.GMAIL,
            status=ConnectorStatus.ACTIVE,
            secret_id=secret.id,
        )
        reviewer = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"delivery-reviewer-{uuid4()}",
            email="reviewer@example.test",
            role=UserRole.REVIEWER,
            status=UserStatus.ACTIVE,
        )
        session.add_all((connector, reviewer))
        await session.flush()
        session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=reviewer.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.review"],
            )
        )
        item = EmailWorkItem(
            organization_id=organization.id,
            connector_id=connector.id,
            knowledge_base_id=knowledge_base.id,
            gmail_message_id=f"delivery-{uuid4().hex}",
            gmail_thread_id="thread-concurrent-delivery",
            sender="customer@example.test",
            recipients=["support@example.test"],
            subject="Need help",
            body="Please help with this request.",
            received_at=datetime(2026, 9, 2, tzinfo=UTC),
            raw_content_ref="gmail://fixture/concurrent-delivery",
            state=EmailState.AWAITING_REVIEW,
            draft_body="Original grounded draft.",
            draft_citations=[],
            draft_provenance={"model": "fixture", "prompt_version": "fixture"},
            version=1,
        )
        session.add(item)
        await session.flush()
        draft = EmailDraftVersion(
            work_item_id=item.id,
            organization_id=organization.id,
            version=1,
            body=item.draft_body,
            to=["customer@example.test"],
            cc=[],
            subject="Re: Need help",
            thread_id=item.gmail_thread_id,
            reviewer_instruction=None,
            model="fixture",
            prompt_version="fixture",
            retrieval_config={},
            citations=[],
            created_by_id=reviewer.id,
            creator_type="STAFF",
        )
        session.add(draft)
        await session.flush()
        item.current_draft_id = draft.id
        principal = Principal(
            reviewer.id,
            organization.id,
            reviewer.email,
            reviewer.role,
            uuid4(),
            "fixture-csrf",
        )
        intent = await _approve(
            session,
            {"item": item, "draft": draft, "principal": principal},
        )
        job_id = intent.job_id
        organization_id = organization.id
        await session.commit()
    return job_id, intent.id, organization_id, principal


@pytest.mark.asyncio
async def test_approval_creates_exactly_one_delivery_intent_and_durable_job(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    intent = await _approve(db_session, email_review_context)

    assert intent.approved_draft_version_id == draft.id
    assert intent.state is EmailState.SEND_PENDING
    assert intent.deterministic_message_id == f"<delivery-{intent.id}@mail.invalid>"
    assert item.state is EmailState.SEND_PENDING
    jobs = list(
        (
            await db_session.scalars(
                select(JobIntent).where(
                    JobIntent.id == intent.job_id,
                    JobIntent.kind == "email.delivery",
                )
            )
        ).all()
    )
    assert len(jobs) == 1
    assert jobs[0].payload == {
        "delivery_intent_id": str(intent.id),
        "organization_id": str(item.organization_id),
        "work_item_id": str(item.id),
    }
    events = list(
        (
            await db_session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "email.delivery.requested",
                    OutboxEvent.aggregate_id == intent.id,
                )
            )
        ).all()
    )
    assert len(events) == 1
    serialized = str(events[0].payload)
    assert item.body not in serialized
    assert draft.body not in serialized


@pytest.mark.asyncio
async def test_delivery_tables_have_minimum_platform_app_privileges(
    db_session: AsyncSession,
) -> None:
    for table_name in ("email_delivery_intents", "email_delivery_attempts"):
        assert await db_session.scalar(
            select(text(f"has_table_privilege('platform_app', '{table_name}', 'SELECT')"))
        )
        assert await db_session.scalar(
            select(text(f"has_table_privilege('platform_app', '{table_name}', 'INSERT')"))
        )
        assert await db_session.scalar(
            select(text(f"has_table_privilege('platform_app', '{table_name}', 'UPDATE')"))
        )
        assert not await db_session.scalar(
            select(text(f"has_table_privilege('platform_app', '{table_name}', 'DELETE')"))
        )
    assert await db_session.scalar(
        select(
            text(
                "has_table_privilege('platform_app', "
                "'email_successful_deliveries', 'SELECT, INSERT')"
            )
        )
    )
    assert not await db_session.scalar(
        select(
            text(
                "has_table_privilege('platform_app', "
                "'email_successful_deliveries', 'UPDATE, DELETE')"
            )
        )
    )


@pytest.mark.asyncio
async def test_successful_send_persists_attempt_and_one_success(
    db_session: AsyncSession,
    email_review_context: dict[str, object],
    delivery_gateway_factory,
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    intent = await _approve(db_session, email_review_context)
    gateway = delivery_gateway_factory()

    result = await EmailDeliveryService(db_session, gateway, worker_id="worker-a").send(
        intent.job_id
    )

    assert result.state is EmailState.SENT
    assert gateway.send_call_count == 1
    attempts = list(
        (
            await db_session.scalars(
                select(DeliveryAttempt).where(DeliveryAttempt.delivery_intent_id == intent.id)
            )
        ).all()
    )
    successes = list(
        (
            await db_session.scalars(
                select(SuccessfulDelivery).where(SuccessfulDelivery.delivery_intent_id == intent.id)
            )
        ).all()
    )
    assert len(attempts) == 1 and attempts[0].outcome == "SENT"
    assert len(successes) == 1
    assert (await db_session.get(JobIntent, intent.job_id)).state is JobState.SUCCEEDED
    assert (await db_session.get(EmailWorkItem, intent.work_item_id)).state is EmailState.SENT
    audit_events = list(
        (
            await db_session.scalars(
                select(AuditEvent).where(AuditEvent.object_id == intent.id)
            )
        ).all()
    )
    outbox_events = list(
        (
            await db_session.scalars(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == intent.id)
            )
        ).all()
    )
    serialized_metadata = str(
        [event.details for event in audit_events] + [event.payload for event in outbox_events]
    )
    assert item.body not in serialized_metadata
    assert draft.body not in serialized_metadata


@pytest.mark.asyncio
async def test_editing_approved_draft_cancels_pending_intent_and_job(
    db_session: AsyncSession,
    email_review_context: dict[str, object],
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    service = EmailReviewService(db_session, email_review_context["principal"])
    intent = await _approve(db_session, email_review_context)

    edited = await service.edit(
        item.id,
        body="Changed after approval.",
        expected_version=item.version,
        current_draft_id=draft.id,
    )

    assert edited.state is EmailState.AWAITING_REVIEW
    assert intent.state is EmailState.FAILED_TERMINAL
    assert intent.last_error_code == "EMAIL_APPROVAL_INVALIDATED"
    job = await db_session.get(JobIntent, intent.job_id)
    assert job is not None and job.state is JobState.FAILED
    assert job.last_error_code == "EMAIL_APPROVAL_INVALIDATED"


@pytest.mark.asyncio
async def test_concurrent_delivery_claim_allows_only_one_provider_send(
    delivery_gateway_factory,
) -> None:
    job_id, intent_id, organization_id, _principal = await _seed_committed_delivery_intent()
    gateway = delivery_gateway_factory()

    try:
        results = await asyncio.gather(
            EmailDeliveryService.from_session_factory(gateway, worker_id="worker-a").send(job_id),
            EmailDeliveryService.from_session_factory(gateway, worker_id="worker-b").send(job_id),
        )
        assert sum(result is not None for result in results) == 1
        assert gateway.send_call_count == 1
        async with async_sessionmaker() as verify:
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(SuccessfulDelivery)
                    .where(SuccessfulDelivery.delivery_intent_id == intent_id)
                )
                == 1
            )
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_send_recovers_from_durable_state_in_fresh_sessions(
    delivery_gateway_factory,
) -> None:
    job_id, intent_id, organization_id, principal = await _seed_committed_delivery_intent()
    gateway = delivery_gateway_factory("timeout_after_send")

    try:
        first = await EmailDeliveryService.from_session_factory(
            gateway, worker_id="worker-a"
        ).send(job_id)
        assert first is not None and first.state is EmailState.DELIVERY_UNKNOWN

        with pytest.raises(ReconciliationRequired):
            await EmailDeliveryService.from_session_factory(
                gateway, worker_id="worker-b"
            ).send(job_id)

        async with async_sessionmaker() as reconcile_session:
            reconciled = await ReconciliationService(
                reconcile_session, gateway, principal
            ).reconcile(intent_id)
            await reconcile_session.commit()
        assert reconciled.state is EmailState.SENT

        async with async_sessionmaker() as verify:
            stored_intent = await verify.get(DeliveryIntent, intent_id)
            stored_job = await verify.get(JobIntent, job_id)
            successful_count = await verify.scalar(
                select(func.count())
                .select_from(SuccessfulDelivery)
                .where(SuccessfulDelivery.delivery_intent_id == intent_id)
            )
            assert stored_intent is not None and stored_intent.state is EmailState.SENT
            assert stored_job is not None and stored_job.state is JobState.SUCCEEDED
            assert successful_count == 1
        assert gateway.send_call_count == 1
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_delivery_job_dispatch_and_consumer_use_encrypted_connector_boundary(
    delivery_gateway_factory,
    monkeypatch,
) -> None:
    job_id, intent_id, organization_id, _principal = await _seed_committed_delivery_intent()
    gateway = delivery_gateway_factory()
    dispatched: list[UUID] = []
    loaded_connector_ids: list[UUID] = []
    supplied_tokens: list[str] = []

    class ConnectorBoundary:
        async def load_refresh_token(self, _db_session, connector):  # type: ignore[no-untyped-def]
            loaded_connector_ids.append(connector.id)
            return "decrypted-only-at-connector-boundary"

    class GatewayFactory:
        async def create(self, *, refresh_token: str) -> GmailConnection:
            supplied_tokens.append(refresh_token)
            return GmailConnection(gateway)

    monkeypatch.setattr(
        "app.modules.email.tasks.email_job.delay",
        lambda job_id: dispatched.append(UUID(job_id)),
    )
    monkeypatch.setattr(
        "app.modules.email.tasks.ConnectorService.from_settings",
        lambda _settings: ConnectorBoundary(),
    )
    monkeypatch.setattr(
        "app.modules.email.tasks.GoogleGmailGatewayFactory.from_settings",
        lambda _settings: GatewayFactory(),
    )

    try:
        await _dispatch_pending_email_jobs()
        assert job_id in dispatched
        await _consume_email_job(job_id)

        async with async_sessionmaker() as verify:
            intent = await verify.get(DeliveryIntent, intent_id)
            assert intent is not None and intent.state is EmailState.SENT
        assert len(loaded_connector_ids) == 1
        assert supplied_tokens == ["decrypted-only-at-connector-boundary"]
        assert gateway.send_call_count == 1
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_delivery_worker_is_fenced_before_attempt_and_provider_io(
    delivery_gateway_factory,
    monkeypatch,
) -> None:
    job_id, intent_id, organization_id, _principal = await _seed_committed_delivery_intent()
    gateway = delivery_gateway_factory()
    worker_a_paused = asyncio.Event()
    worker_b_paused = asyncio.Event()
    release_worker_a = asyncio.Event()
    release_worker_b = asyncio.Event()
    calls: dict[str, int] = {}
    original = EmailDeliveryService._intent_for_job

    async def pause_after_durable_claim(
        service: EmailDeliveryService, running_job: JobIntent
    ) -> DeliveryIntent:
        intent = await original(service, running_job)
        worker_id = service._worker_id
        calls[worker_id] = calls.get(worker_id, 0) + 1
        if calls[worker_id] == 2:
            if worker_id == "delivery-worker-a":
                worker_a_paused.set()
                await release_worker_a.wait()
            elif worker_id == "delivery-worker-b":
                worker_b_paused.set()
                await release_worker_b.wait()
        return intent

    monkeypatch.setattr(EmailDeliveryService, "_intent_for_job", pause_after_durable_claim)
    worker_a = EmailDeliveryService(
        None,
        gateway,
        worker_id="delivery-worker-a",
        lease_seconds=1,
    )
    worker_b = EmailDeliveryService(
        None,
        gateway,
        worker_id="delivery-worker-b",
        lease_seconds=30,
    )
    first = asyncio.create_task(worker_a.send(job_id))
    second: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(worker_a_paused.wait(), timeout=3)
        await asyncio.sleep(1.2)
        second = asyncio.create_task(worker_b.send(job_id))
        await asyncio.wait_for(worker_b_paused.wait(), timeout=3)

        release_worker_a.set()
        with pytest.raises(JobLeaseLost):
            await asyncio.wait_for(first, timeout=3)
        assert gateway.send_call_count == 0

        release_worker_b.set()
        result = await asyncio.wait_for(second, timeout=3)
        assert result is not None and result.state is EmailState.SENT
        assert gateway.send_call_count == 1

        async with async_sessionmaker() as verify:
            stored_intent = await verify.get(DeliveryIntent, intent_id)
            attempts = await verify.scalar(
                select(func.count())
                .select_from(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_intent_id == intent_id)
            )
            assert stored_intent is not None and stored_intent.state is EmailState.SENT
            assert attempts == 1
    finally:
        release_worker_a.set()
        release_worker_b.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_worker_after_attempt_is_fenced_before_gmail_call(
    delivery_gateway_factory,
    monkeypatch,
) -> None:
    job_id, intent_id, organization_id, _principal = await _seed_committed_delivery_intent()
    gateway = delivery_gateway_factory()
    attempt_committed = asyncio.Event()
    release_worker_a = asyncio.Event()
    original = EmailDeliveryService._resolve_gateway

    async def pause_before_provider(
        service: EmailDeliveryService, item: EmailWorkItem
    ) -> object:
        resolved = await original(service, item)
        if service._worker_id == "delivery-provider-worker-a":
            attempt_committed.set()
            await release_worker_a.wait()
        return resolved

    monkeypatch.setattr(EmailDeliveryService, "_resolve_gateway", pause_before_provider)
    first = asyncio.create_task(
        EmailDeliveryService(
            None,
            gateway,
            worker_id="delivery-provider-worker-a",
            lease_seconds=1,
        ).send(job_id)
    )
    try:
        await asyncio.wait_for(attempt_committed.wait(), timeout=3)
        await asyncio.sleep(1.2)
        takeover = await EmailDeliveryService(
            None,
            gateway,
            worker_id="delivery-provider-worker-b",
            lease_seconds=30,
        ).send(job_id)
        assert takeover is not None and takeover.state is EmailState.DELIVERY_UNKNOWN

        release_worker_a.set()
        with pytest.raises(JobLeaseLost):
            await asyncio.wait_for(first, timeout=3)
        assert gateway.send_call_count == 0

        async with async_sessionmaker() as verify:
            intent = await verify.get(DeliveryIntent, intent_id)
            attempts = list(
                (
                    await verify.scalars(
                        select(DeliveryAttempt).where(
                            DeliveryAttempt.delivery_intent_id == intent_id
                        )
                    )
                ).all()
            )
            assert intent is not None and intent.state is EmailState.DELIVERY_UNKNOWN
            assert len(attempts) == 1 and attempts[0].outcome == "UNKNOWN"
    finally:
        release_worker_a.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()


def test_email_delivery_outbox_tasks_are_registered() -> None:
    assert (
        email_delivery_outbox_event.name
        == "app.modules.email.tasks.email_delivery_outbox_event"
    )
    assert (
        dispatch_pending_email_delivery_outbox_events.name
        == "app.modules.email.tasks.dispatch_pending_email_delivery_outbox_events"
    )
    assert email_delivery_outbox_event.autoretry_for == (Exception,)
    assert email_delivery_outbox_event.max_retries == 5
    schedule = create_celery().conf.beat_schedule["email-delivery-outbox-dispatch"]
    assert (
        schedule["task"]
        == "app.modules.email.tasks.dispatch_pending_email_delivery_outbox_events"
    )


@pytest.mark.asyncio
async def test_email_delivery_outbox_broker_failure_stays_pending_for_retry(
    db_session: AsyncSession,
    email_review_context: dict[str, object],
    monkeypatch,
) -> None:
    intent = await _approve(db_session, email_review_context)
    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "email.delivery.requested",
            OutboxEvent.aggregate_id == intent.id,
        )
    )
    assert event is not None
    event_id = event.event_id
    await db_session.commit()

    def broker_down(_event_id: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(email_delivery_outbox_event, "delay", broker_down)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        await _dispatch_email_delivery_outbox_event(event_id, db_session=db_session)
    db_session.expire_all()
    pending = await db_session.get(OutboxEvent, event_id)
    assert pending is not None
    assert pending.published_at is None
    assert pending.publish_attempts == 1

    accepted: list[str] = []
    monkeypatch.setattr(email_delivery_outbox_event, "delay", accepted.append)
    assert await _dispatch_email_delivery_outbox_event(event_id, db_session=db_session)
    db_session.expire_all()
    delivered = await db_session.get(OutboxEvent, event_id)
    assert accepted == [str(event_id)]
    assert delivered is not None and delivered.published_at is not None
    assert delivered.publish_attempts == 2


@pytest.mark.asyncio
async def test_pending_delivery_outbox_survives_restart_and_is_dispatched(
    delivery_gateway_factory,
    monkeypatch,
) -> None:
    _job_id, intent_id, organization_id, _principal = await _seed_committed_delivery_intent()
    async with async_sessionmaker() as lookup:
        event_id = await lookup.scalar(
            select(OutboxEvent.event_id).where(
                OutboxEvent.event_type == "email.delivery.requested",
                OutboxEvent.aggregate_id == intent_id,
            )
        )
    assert event_id is not None
    accepted: list[str] = []
    monkeypatch.setattr(email_delivery_outbox_event, "delay", accepted.append)

    try:
        await _dispatch_pending_email_delivery_outbox_events()
        async with async_sessionmaker() as verify:
            stored = await verify.get(OutboxEvent, event_id)
            assert stored is not None and stored.published_at is not None
        assert accepted.count(str(event_id)) == 1
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_delivery_outbox_consumption_has_one_durable_outcome(
    delivery_gateway_factory,
    monkeypatch,
) -> None:
    job_id, intent_id, organization_id, _principal = await _seed_committed_delivery_intent()
    gateway = delivery_gateway_factory()
    loaded_connector_ids: list[UUID] = []

    class ConnectorBoundary:
        async def load_refresh_token(self, _db_session, connector):  # type: ignore[no-untyped-def]
            loaded_connector_ids.append(connector.id)
            return "decrypted-only-at-connector-boundary"

    class GatewayFactory:
        async def create(self, *, refresh_token: str) -> GmailConnection:
            assert refresh_token == "decrypted-only-at-connector-boundary"
            return GmailConnection(gateway)

    monkeypatch.setattr(
        "app.modules.email.tasks.ConnectorService.from_settings",
        lambda _settings: ConnectorBoundary(),
    )
    monkeypatch.setattr(
        "app.modules.email.tasks.GoogleGmailGatewayFactory.from_settings",
        lambda _settings: GatewayFactory(),
    )
    async with async_sessionmaker() as lookup:
        event_id = await lookup.scalar(
            select(OutboxEvent.event_id).where(
                OutboxEvent.event_type == "email.delivery.requested",
                OutboxEvent.aggregate_id == intent_id,
            )
        )
    assert event_id is not None

    try:
        outcomes = await asyncio.gather(
            _consume_email_delivery_outbox_event(event_id),
            _consume_email_delivery_outbox_event(event_id),
        )
        assert sorted(outcomes) == [False, True]
        async with async_sessionmaker() as verify:
            processed = await verify.scalar(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(
                    ProcessedEvent.consumer_name == "email-delivery-requested-v1",
                    ProcessedEvent.event_id == event_id,
                )
            )
            job = await verify.get(JobIntent, job_id)
            intent = await verify.get(DeliveryIntent, intent_id)
            assert processed == 1
            assert job is not None and job.state is JobState.SUCCEEDED
            assert intent is not None and intent.state is EmailState.SENT
        assert gateway.send_call_count == 1
        assert len(loaded_connector_ids) == 1
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()
