from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.models import ConnectorStatus
from app.modules.email.classification import ClassificationExecution
from app.modules.email.gmail_gateway import GmailAuthorizationError, GmailHistoryPage, GmailMessage
from app.modules.email.ingestion import EmailIngestionService
from app.modules.email.models import (
    EmailCategory,
    EmailPriority,
    EmailState,
    EmailStateHistory,
    EmailSyncState,
    EmailWorkItem,
)
from app.modules.email.schemas import EmailClassification
from app.modules.jobs.models import JobIntent
from app.modules.jobs.service import JobService
from app.modules.outbox.models import OutboxEvent


class FixedClassifier:
    async def classify(self, _subject: str, _body: str) -> ClassificationExecution:
        return ClassificationExecution(
            EmailClassification(
                category=EmailCategory.ACTION_REQUIRED,
                priority=EmailPriority.HIGH,
                reply_required=True,
            ),
            "claude-fixture",
            "email-classification-v1",
            3,
            10,
            4,
            0.00009,
        )


class FailingClassifier:
    async def classify(self, _subject: str, _body: str) -> ClassificationExecution:
        raise RuntimeError("provider payload must not escape")


class FakeGmailGateway:
    def __init__(self, messages: list[GmailMessage]) -> None:
        self._messages = {message.id: message for message in messages}
        self.get_calls: list[str] = []

    async def list_history(
        self, _start_history_id: str | None, _page_token: str | None = None
    ) -> GmailHistoryPage:
        return GmailHistoryPage(tuple(self._messages), "102", None)

    async def get_message(self, message_id: str) -> GmailMessage:
        self.get_calls.append(message_id)
        return self._messages[message_id]


class FailingSecondMessageGateway(FakeGmailGateway):
    async def get_message(self, message_id: str) -> GmailMessage:
        if self.get_calls:
            raise RuntimeError("temporary Gmail page failure")
        return await super().get_message(message_id)


class RevokedGateway:
    def __init__(self) -> None:
        self.get_calls = 0

    async def list_history(
        self, _start_history_id: str | None, _page_token: str | None = None
    ) -> GmailHistoryPage:
        raise GmailAuthorizationError("revoked fixture credential")

    async def get_message(self, _message_id: str) -> GmailMessage:
        self.get_calls += 1
        raise AssertionError("revoked connectors must not fetch messages")


def _second_message(first: GmailMessage) -> GmailMessage:
    return GmailMessage(
        id=f"gmail-{uuid4().hex}",
        thread_id="thread-8",
        history_id="101",
        sender="second@example.test",
        recipients=("support@example.test",),
        subject="Second message",
        body="Please reply.",
        received_at=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
        raw_content_ref=f"gmail://fixture/{uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_cursor_does_not_advance_when_any_message_in_page_rolls_back(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    connector_id = connector.id
    organization_id = connector.organization_id
    gateway = FailingSecondMessageGateway(
        [email_context["message"], _second_message(email_context["message"])]
    )

    with pytest.raises(RuntimeError, match="temporary Gmail page failure"):
        await EmailIngestionService(
            db_session, gateway=gateway, classifier=FixedClassifier()
        ).ingest_history(connector_id, knowledge_base.id)

    assert (
        await db_session.scalar(
            select(func.count(EmailWorkItem.id)).where(
                EmailWorkItem.organization_id == organization_id
            )
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(EmailSyncState.history_id).where(EmailSyncState.connector_id == connector_id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_history_worker_can_defer_page_commit_until_lease_completion(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    connector_id = connector.id
    organization_id = connector.organization_id

    await EmailIngestionService(
        db_session,
        gateway=FakeGmailGateway([email_context["message"]]),
        classifier=FixedClassifier(),
    ).ingest_history(connector_id, knowledge_base.id, commit=False)

    assert db_session.in_transaction()
    await db_session.rollback()
    assert (
        await db_session.scalar(
            select(func.count(EmailWorkItem.id)).where(
                EmailWorkItem.organization_id == organization_id
            )
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count(EmailSyncState.id)).where(EmailSyncState.connector_id == connector_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_automated_ingestion_history_records_system_actor_and_job(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    job = await JobService().enqueue(
        db_session,
        "email.gmail_history",
        f"history-provenance-{uuid4()}",
        {"connector_id": str(connector.id)},
    )

    await EmailIngestionService(
        db_session,
        gateway=FakeGmailGateway([email_context["message"]]),
        classifier=FixedClassifier(),
    ).ingest_history(connector.id, knowledge_base.id, job_id=job.id)

    histories = list((await db_session.scalars(select(EmailStateHistory))).all())
    assert histories
    assert {history.job_id for history in histories} == {job.id}
    assert {history.actor_type for history in histories} == {"SYSTEM"}
    assert all(history.actor_id is not None for history in histories)


@pytest.mark.asyncio
async def test_retry_job_survives_classification_failure_without_sensitive_error(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    item = await EmailIngestionService(db_session, classifier=FailingClassifier()).ingest_message(
        email_context["message"],
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )

    assert item.state is EmailState.DRAFT_RETRY_WAIT
    assert item.last_error_code == "EMAIL_CLASSIFICATION_FAILED"
    job = await db_session.scalar(select(JobIntent).where(JobIntent.kind == "email.classify"))
    assert job is not None
    assert job.payload == {
        "work_item_id": str(item.id),
        "organization_id": str(item.organization_id),
        "connector_id": str(item.connector_id),
        "knowledge_base_id": str(item.knowledge_base_id),
        "phase": "classification",
    }
    assert "provider payload" not in str(job.payload)


@pytest.mark.asyncio
async def test_manual_retry_uses_state_machine_and_creates_new_durable_intent(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    service = EmailIngestionService(db_session, classifier=FailingClassifier())
    item = await service.ingest_message(
        email_context["message"],
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )
    original_job_count = await db_session.scalar(select(func.count(JobIntent.id)))

    retried = await service.retry_failed_work_item(item.id)

    assert retried.state is EmailState.DRAFTING
    assert retried.last_error_code is None
    assert await db_session.scalar(select(func.count(JobIntent.id))) == original_job_count + 1


@pytest.mark.asyncio
async def test_revoked_connector_stops_ingestion_and_records_safe_admin_error(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    gateway = RevokedGateway()

    result = await EmailIngestionService(
        db_session, gateway=gateway, classifier=FixedClassifier()
    ).ingest_history(connector.id, knowledge_base.id)

    await db_session.refresh(connector)
    assert result.reauth_required is True
    assert connector.status is ConnectorStatus.REAUTH_REQUIRED
    assert gateway.get_calls == 0
    sync_state = await db_session.scalar(
        select(EmailSyncState).where(EmailSyncState.connector_id == connector.id)
    )
    assert sync_state is not None
    assert sync_state.last_error_code == "GMAIL_REAUTH_REQUIRED"
    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "connector.reauthorization_required",
            OutboxEvent.aggregate_id == connector.id,
        )
    )
    assert event is not None
    assert event.payload == {
        "organization_id": str(connector.organization_id),
        "kind": "GMAIL",
        "error_code": "GMAIL_REAUTH_REQUIRED",
    }
