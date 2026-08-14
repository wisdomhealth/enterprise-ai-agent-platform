from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.outbox.models import OutboxEvent

type EventPublisher = Callable[[OutboxEvent], Awaitable[None]]


class OutboxDispatcher:
    def __init__(self, publish: EventPublisher, *, batch_size: int = 100) -> None:
        self._publish = publish
        self._batch_size = batch_size

    async def dispatch_pending(self, db_session: AsyncSession) -> int:
        events = (
            await db_session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.occurred_at, OutboxEvent.event_id)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        published_count = 0
        for event in events:
            event.publish_attempts += 1
            try:
                await self._publish(event)
            except Exception:
                continue
            event.published_at = datetime.now(UTC)
            published_count += 1
        await db_session.flush()
        return published_count
