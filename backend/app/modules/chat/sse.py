"""Recoverable PostgreSQL-first SSE for public chat."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ChatSSEEventType
from app.modules.outbox.models import OutboxEvent


@dataclass(frozen=True, slots=True)
class ChatSSEEvent:
    sequence: int
    event: str
    data: dict[str, object]


class RedisChatEventPublisher:
    """Ephemeral wake-up hints only; durable content stays in PostgreSQL."""

    def __init__(self, redis_client: object) -> None:
        self._redis_client = redis_client

    async def publish(self, session_id: UUID, sequence: int) -> None:
        publish = getattr(self._redis_client, "publish")
        await publish(_channel(session_id), str(sequence))


class PostgresSSEService:
    def __init__(self, db_session: AsyncSession, *, redis_client: object | None = None) -> None:
        self._db_session = db_session
        self._redis_client = redis_client

    async def events_after(self, session_id: UUID, *, after_sequence: int) -> list[ChatSSEEvent]:
        messages = list(
            (
                await self._db_session.scalars(
                    select(ChatMessage)
                    .where(
                        ChatMessage.session_id == session_id,
                        ChatMessage.sequence > after_sequence,
                        ChatMessage.actor.in_([ChatActor.AI, ChatActor.SYSTEM]),
                    )
                    .order_by(ChatMessage.sequence)
                )
            ).all()
        )
        published = list(
            (
                await self._db_session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_type == "chat_session",
                        OutboxEvent.aggregate_id == session_id,
                        OutboxEvent.event_type.in_(
                            ["chat.answer.validated", "chat.answer.refused"]
                        ),
                    )
                )
            ).all()
        )
        answer_metadata = {
            str(event.payload.get("message_id")): event.payload for event in published
        }
        return [
            self._validated_event(message, answer_metadata.get(str(message.id)))
            for message in messages
        ]

    async def stream(self, session_id: UUID, *, after_sequence: int) -> AsyncIterator[str]:
        highest = after_sequence
        session = await self._db_session.get(ChatSession, session_id)
        if isinstance(session, ChatSession):
            yield _encode(
                ChatSSEEvent(
                    sequence=highest,
                    event=ChatSSEEventType.SESSION_STATE.value,
                    data={"state": session.state.value, "version": session.version},
                )
            )
        for event in await self.events_after(session_id, after_sequence=highest):
            highest = max(highest, event.sequence)
            yield _encode(event)
            for segment in _segments(event):
                yield _encode(segment)
        if self._redis_client is None:
            return
        pubsub_factory = getattr(self._redis_client, "pubsub", None)
        if pubsub_factory is None:
            return
        pubsub = pubsub_factory()
        try:
            await pubsub.subscribe(_channel(session_id))
            while True:
                hint = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if hint is None:
                    yield ": keepalive\n\n"
                    continue
                for event in await self.events_after(session_id, after_sequence=highest):
                    highest = max(highest, event.sequence)
                    yield _encode(event)
                    for segment in _segments(event):
                        yield _encode(segment)
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(_channel(session_id))
            await pubsub.close()

    @staticmethod
    def _validated_event(
        message: ChatMessage, metadata: dict[str, object] | None = None
    ) -> ChatSSEEvent:
        if message.actor is ChatActor.SYSTEM or (metadata or {}).get("refused") is True:
            return ChatSSEEvent(
                sequence=message.sequence,
                event=ChatSSEEventType.ERROR_SAFE.value,
                data={
                    "sequence": message.sequence,
                    "body": message.body,
                    "handoff_recommended": bool(
                        (metadata or {}).get("handoff_recommended", True)
                    ),
                },
            )
        raw_citations = (metadata or {}).get("citations", [])
        raw_segments = (metadata or {}).get("segments", [message.body])
        citations = list(raw_citations) if isinstance(raw_citations, list) else []
        segments = list(raw_segments) if isinstance(raw_segments, list) else [message.body]
        return ChatSSEEvent(
            sequence=message.sequence,
            event=ChatSSEEventType.MESSAGE_VALIDATED.value,
            data={
                "sequence": message.sequence,
                "body": message.body,
                "citations": citations,
                "segments": segments,
            },
        )


def _segments(event: ChatSSEEvent) -> list[ChatSSEEvent]:
    if event.event != ChatSSEEventType.MESSAGE_VALIDATED.value:
        return []
    raw_segments = event.data.get("segments")
    segments = (
        [str(segment) for segment in raw_segments]
        if isinstance(raw_segments, list) and raw_segments
        else [str(event.data["body"])]
    )
    return [
        ChatSSEEvent(
            sequence=event.sequence,
            event=ChatSSEEventType.MESSAGE_SEGMENT.value,
            data={"sequence": event.sequence, "index": index, "text": segment},
        )
        for index, segment in enumerate(segments)
    ]


def _channel(session_id: UUID) -> str:
    return f"chat:sse:{session_id}"


def _encode(event: ChatSSEEvent) -> str:
    return (
        f"id: {event.sequence}\n"
        f"event: {event.event}\n"
        f"data: {json.dumps(event.data, separators=(',', ':'))}\n\n"
    )
