from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.gmail_gateway import (
    GmailConnection,
    GmailGateway,
    GmailProfile,
    GmailWatchResult,
)
from app.modules.email.models import EmailSyncState, GmailWatchStatus
from app.modules.email.push import GmailPushService, VerifiedGmailNotification
from app.modules.email.watch import GmailWatchService
from app.modules.jobs.models import JobIntent


class _ConnectorService:
    async def load_refresh_token(self, _db_session, _connector):  # type: ignore[no-untyped-def]
        return "refresh-token"


class _Factory:
    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
        self.client = client

    async def create(self, *, refresh_token: str) -> GmailConnection:
        assert refresh_token == "refresh-token"
        return GmailConnection(GmailGateway(self.client))


class _WatchClient:
    def __init__(self, on_watch=None) -> None:  # type: ignore[no-untyped-def]
        self.on_watch = on_watch
        self.stopped = False

    async def get_profile(self) -> GmailProfile:
        return GmailProfile("Staff@Example.test", "100")

    async def start_watch(self, topic_name: str) -> GmailWatchResult:
        assert topic_name == "projects/p/topics/gmail"
        if self.on_watch is not None:
            await self.on_watch()
        return GmailWatchResult("101", datetime.now(UTC) + timedelta(days=1))

    async def stop_watch(self) -> None:
        self.stopped = True


def _notification(message_id: str) -> VerifiedGmailNotification:
    return VerifiedGmailNotification(
        subscription_name="projects/p/subscriptions/gmail",
        pubsub_message_id=message_id,
        published_at=datetime.now(UTC),
        mailbox_address="staff@example.test",
        history_id="101",
    )


@pytest.mark.asyncio
async def test_immediate_notification_during_start_is_durable(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    connector_id = connector.id
    organization_id = connector.organization_id

    async def immediate_push() -> None:
        await GmailPushService(db_session, receipt_retention_days=14).accept(
            _notification("watch-immediate")
        )

    service = GmailWatchService(
        db_session,
        connector_service=_ConnectorService(),  # type: ignore[arg-type]
        gateway_factory=_Factory(_WatchClient(immediate_push)),  # type: ignore[arg-type]
        topic_name="projects/p/topics/gmail",
    )

    state = await service.start(connector_id, organization_id)

    assert state.watch_status is GmailWatchStatus.ACTIVE
    assert state.auto_sync_enabled is True
    assert state.mailbox_address == "staff@example.test"
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.payload["connector_id"].astext == str(connector_id)
        )
    ) == 1


@pytest.mark.asyncio
async def test_stop_prevents_delayed_notification_from_starting_work(
    db_session: AsyncSession, email_context: dict[str, object]
) -> None:
    connector = email_context["connector"]
    connector_id = connector.id
    organization_id = connector.organization_id
    state = EmailSyncState(
        organization_id=organization_id,
        connector_id=connector_id,
        mailbox_address="staff@example.test",
        processed_history_id="100",
        auto_sync_enabled=True,
        watch_status=GmailWatchStatus.ACTIVE,
    )
    db_session.add(state)
    await db_session.commit()
    client = _WatchClient()
    service = GmailWatchService(
        db_session,
        connector_service=_ConnectorService(),  # type: ignore[arg-type]
        gateway_factory=_Factory(client),  # type: ignore[arg-type]
        topic_name="projects/p/topics/gmail",
    )

    stopped = await service.stop(connector_id, organization_id)
    result = await GmailPushService(db_session, receipt_retention_days=14).accept(
        _notification("after-stop")
    )

    assert client.stopped is True
    assert stopped.watch_status is GmailWatchStatus.STOPPED
    assert stopped.auto_sync_enabled is False
    assert result is None
    assert await db_session.scalar(
        select(func.count(JobIntent.id)).where(
            JobIntent.payload["connector_id"].astext == str(connector_id)
        )
    ) == 0
