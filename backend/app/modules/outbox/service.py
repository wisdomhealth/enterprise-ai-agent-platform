from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.outbox.models import OutboxEvent, ProcessedEvent


class OutboxService:
    async def add(
        self,
        db_session: AsyncSession,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: Mapping[str, object],
        *,
        event_version: int = 1,
    ) -> OutboxEvent:
        event = OutboxEvent(
            event_type=event_type,
            event_version=event_version,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=dict(payload),
        )
        db_session.add(event)
        await db_session.flush()
        return event

    async def begin_processing(
        self,
        db_session: AsyncSession,
        consumer_name: str,
        event_id: UUID,
    ) -> bool:
        inserted_event_id = await db_session.scalar(
            insert(ProcessedEvent)
            .values(consumer_name=consumer_name, event_id=event_id)
            .on_conflict_do_nothing(
                index_elements=[ProcessedEvent.consumer_name, ProcessedEvent.event_id]
            )
            .returning(ProcessedEvent.event_id)
        )
        return inserted_event_id is not None
