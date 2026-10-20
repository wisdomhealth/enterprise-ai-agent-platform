import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.classification import ClassificationExecution
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
)
from app.modules.email.schemas import EmailClassification
from app.modules.jobs.models import JobIntent

pytestmark = pytest.mark.asyncio


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


class FakeGmailGateway:
    def __init__(self, messages: list[GmailMessage]) -> None:
        self._messages = {message.id: message for message in messages}
        self.list_calls: list[tuple[str | None, str | None]] = []

    async def get_profile(self) -> GmailProfile:
        return GmailProfile("support@example.test", "101")

    async def list_recent_inbox(
        self, *, after, page_token: str | None, max_results: int  # type: ignore[no-untyped-def]
    ) -> GmailMessagePage:
        del after, page_token, max_results
        return GmailMessagePage(tuple(self._messages), None)

    async def list_history(
        self, start_history_id: str | None, page_token: str | None = None
    ) -> GmailHistoryPage:
        self.list_calls.append((start_history_id, page_token))
        return GmailHistoryPage(tuple(self._messages), "102", None)

    async def get_message(self, message_id: str) -> GmailMessage:
        return self._messages[message_id]


class PaginatedBootstrapGateway(FakeGmailGateway):
    def __init__(self, messages: list[GmailMessage]) -> None:
        super().__init__(messages)
        self.recent_calls = 0

    async def list_recent_inbox(
        self, *, after, page_token: str | None, max_results: int  # type: ignore[no-untyped-def]
    ) -> GmailMessagePage:
        del after, max_results
        self.recent_calls += 1
        if page_token is None:
            return GmailMessagePage(tuple(self._messages), "recent-page-2")
        assert page_token == "recent-page-2"
        return GmailMessagePage((), None)

    async def list_history(
        self, start_history_id: str | None, page_token: str | None = None
    ) -> GmailHistoryPage:
        self.list_calls.append((start_history_id, page_token))
        assert (start_history_id, page_token) == ("101", None)
        return GmailHistoryPage((), "102", None)


async def test_duplicate_gmail_message_creates_one_work_item(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    message = email_context["message"]
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    service = EmailIngestionService(db_session, classifier=FixedClassifier())

    first = await service.ingest_message(
        message,
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )
    duplicate = await service.ingest_message(
        message,
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )

    assert first.id == duplicate.id
    assert (
        await db_session.scalar(
            select(func.count(EmailWorkItem.id)).where(
                EmailWorkItem.organization_id == connector.organization_id,
                EmailWorkItem.gmail_message_id == message.id,
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count(JobIntent.id)).where(
                JobIntent.kind == "email.classify",
                JobIntent.payload["work_item_id"].astext == str(first.id),
            )
        )
        == 1
    )


async def test_ingestion_normalizes_headers_body_and_queues_durable_classification(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    message = email_context["message"]
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    item = await EmailIngestionService(db_session, classifier=FixedClassifier()).ingest_message(
        message,
        organization_id=connector.organization_id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
    )

    assert item.sender == "Customer <customer@example.test>"
    assert item.recipients == ["Support <support@example.test>", "other@example.test"]
    assert item.subject == "Need refund help"
    assert item.body == "First line.\n\nSecond line."
    assert item.state is EmailState.INGESTED
    job = await db_session.scalar(
        select(JobIntent).where(
            JobIntent.kind == "email.classify",
            JobIntent.payload["work_item_id"].astext == str(item.id),
        )
    )
    assert job is not None
    assert job.payload == {
        "work_item_id": str(item.id),
        "organization_id": str(item.organization_id),
        "connector_id": str(item.connector_id),
        "knowledge_base_id": str(item.knowledge_base_id),
        "phase": "classification",
    }


async def test_history_page_persists_messages_and_cursor_together(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    gateway = FakeGmailGateway([email_context["message"]])

    result = await EmailIngestionService(
        db_session, gateway=gateway, classifier=FixedClassifier()
    ).ingest_history(connector.id, knowledge_base.id)

    assert result.history_id == "102"
    assert result.ingested == 1
    assert gateway.list_calls == [("101", None)]
    assert (
        await db_session.scalar(
            select(func.count(EmailWorkItem.id)).where(EmailWorkItem.connector_id == connector.id)
        )
        == 1
    )


async def test_empty_increment_advances_cursor_without_downstream_jobs(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    state = EmailSyncState(
        organization_id=connector.organization_id,
        connector_id=connector.id,
        processed_history_id="101",
    )
    db_session.add(state)
    await db_session.commit()
    downstream_jobs_before = await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind.in_(("email.classify", "email.draft"))
        )
    )

    result = await EmailIngestionService(
        db_session,
        gateway=FakeGmailGateway([]),
        classifier=FixedClassifier(),
    ).ingest_history(connector.id, knowledge_base.id)

    await db_session.refresh(state)
    assert result.ingested == 0
    assert state.processed_history_id == "102"
    assert state.last_success_at is None
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.kind.in_(("email.classify", "email.draft"))
        )
    ) == downstream_jobs_before


async def test_bootstrap_anchor_is_committed_with_each_page_for_crash_safe_resume(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    knowledge_base = email_context["knowledge_base"]
    gateway = PaginatedBootstrapGateway([email_context["message"]])
    service = EmailIngestionService(db_session, gateway=gateway, classifier=FixedClassifier())

    result = await service.ingest_history(connector.id, knowledge_base.id)

    assert result.history_id == "102"
    assert result.next_page_token is None
    assert gateway.recent_calls == 2
    assert gateway.list_calls == [("101", None)]
