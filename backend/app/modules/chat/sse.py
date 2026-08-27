"""Recoverable PostgreSQL-first SSE for public chat.

An SSE cursor names one durable event, rather than merely a chat-message
sequence.  That matters for a reconnect in the middle of a sentence-segment
stream: ``2:s:0`` resumes at ``2:s:1`` without either replaying the first
segment or dropping the remainder of message 2.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ChatSSEEventType
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.types import CustomerCitation


@dataclass(frozen=True, slots=True)
class ChatSSEEvent:
    cursor: str
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

    async def events_after(
        self,
        session_id: UUID,
        *,
        after_cursor: str | int = "0",
        after_sequence: int | None = None,
    ) -> list[ChatSSEEvent]:
        """Read every customer-visible answer event from durable PostgreSQL.

        Outbox payloads hold the validated sentence segments and citations; no
        provider token stream or Redis state contributes customer-visible text.
        """
        if after_sequence is not None:
            after_cursor = after_sequence
        messages = list(
            (
                await self._db_session.scalars(
                    select(ChatMessage)
                    .where(
                        ChatMessage.session_id == session_id,
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
                            [
                                "chat.answer.validated",
                                "chat.answer.refused",
                                "chat.answer.safe_error",
                            ]
                        ),
                    )
                )
            ).all()
        )
        provenance_by_message_id: dict[str, list[OutboxEvent]] = {}
        for event in published:
            message_id = event.payload.get("message_id")
            if isinstance(message_id, str):
                provenance_by_message_id.setdefault(message_id, []).append(event)
        all_events: list[ChatSSEEvent] = []
        for message in messages:
            metadata = _approved_customer_metadata(
                message, provenance_by_message_id.get(str(message.id), [])
            )
            if metadata is None:
                # A chat_messages row by itself proves neither grounded
                # validation nor an approved safe error.  Never use a body as
                # a fallback provenance record.
                continue
            validated = self._validated_event(message, metadata)
            all_events.append(validated)
            if validated.event == ChatSSEEventType.MESSAGE_VALIDATED.value:
                all_events.extend(_segment_events(message, metadata))
        return [
            event
            for event in all_events
            if _cursor_sort_key(event.cursor) > _cursor_sort_key(str(after_cursor))
        ]

    async def stream(
        self,
        session_id: UUID,
        *,
        after_cursor: str | int = "0",
        after_sequence: int | None = None,
    ) -> AsyncIterator[str]:
        if after_sequence is not None:
            after_cursor = after_sequence
        highest = str(after_cursor)
        session = await self._db_session.get(ChatSession, session_id)
        if isinstance(session, ChatSession):
            yield _encode(
                ChatSSEEvent(
                    cursor=f"0:t:{session.version}",
                    sequence=0,
                    event=ChatSSEEventType.SESSION_STATE.value,
                    data={
                        "sequence": 0,
                        "state": session.state.value,
                        "version": session.version,
                    },
                )
            )
        for event in await self.events_after(session_id, after_cursor=highest):
            highest = event.cursor
            yield _encode(event)
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
                for event in await self.events_after(session_id, after_cursor=highest):
                    highest = event.cursor
                    yield _encode(event)
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(_channel(session_id))
            await pubsub.close()

    @staticmethod
    def _validated_event(
        message: ChatMessage, metadata: dict[str, object]
    ) -> ChatSSEEvent:
        if message.actor is ChatActor.SYSTEM or metadata.get("refused") is True:
            safe_body = _approved_safe_body(metadata)
            assert safe_body is not None
            return ChatSSEEvent(
                cursor=f"{message.sequence}:e:0",
                sequence=message.sequence,
                event=ChatSSEEventType.ERROR_SAFE.value,
                data={
                    "sequence": message.sequence,
                    "body": safe_body,
                    "handoff_recommended": bool(metadata["handoff_recommended"]),
                },
            )
        citations = _approved_customer_citations(metadata)
        segments = _approved_segments(metadata)
        assert citations is not None
        assert segments is not None
        # The complete answer body intentionally is not released in this
        # event.  Customer-visible text travels only in durable, individually
        # resumable segments below.
        return ChatSSEEvent(
            cursor=f"{message.sequence}:v:0",
            sequence=message.sequence,
            event=ChatSSEEventType.MESSAGE_VALIDATED.value,
            data={
                "sequence": message.sequence,
                "citations": citations,
                "segment_count": len(segments),
            },
        )


def _segment_events(message: ChatMessage, metadata: dict[str, object] | None) -> list[ChatSSEEvent]:
    assert metadata is not None
    segments = _approved_segments(metadata)
    assert segments is not None
    return [
        ChatSSEEvent(
            cursor=f"{message.sequence}:s:{index}",
            sequence=message.sequence,
            event=ChatSSEEventType.MESSAGE_SEGMENT.value,
            data={"sequence": message.sequence, "index": index, "text": segment},
        )
        for index, segment in enumerate(segments)
    ]


def _approved_customer_metadata(
    message: ChatMessage, candidates: list[OutboxEvent]
) -> dict[str, object] | None:
    """Return one durable record that authorizes this exact visible message."""
    approved = [
        event
        for event in candidates
        if _is_approved_provenance(message, event)
    ]
    if len(approved) != 1:
        return None
    return approved[0].payload


def _is_approved_provenance(message: ChatMessage, event: OutboxEvent) -> bool:
    payload = event.payload
    if (
        payload.get("message_id") != str(message.id)
        or payload.get("sequence") != message.sequence
    ):
        return False
    if message.actor is ChatActor.SYSTEM:
        return (
            event.event_type == "chat.answer.safe_error"
            and isinstance(payload.get("code"), str)
            and isinstance(payload.get("handoff_recommended"), bool)
            and _approved_safe_body(payload) == message.body
        )
    if message.actor is not ChatActor.AI:
        return False
    if event.event_type == "chat.answer.validated":
        if payload.get("refused", False) is not False:
            return False
    elif event.event_type == "chat.answer.refused":
        if payload.get("refused") is not True:
            return False
        if _approved_safe_body(payload) != message.body:
            return False
    else:
        return False
    return (
        _approved_customer_citations(payload) is not None
        and _approved_segments(payload) is not None
    )


def _approved_safe_body(payload: dict[str, object]) -> str | None:
    safe_body = payload.get("safe_body")
    return safe_body if isinstance(safe_body, str) and safe_body.strip() else None


def _approved_customer_citations(payload: dict[str, object]) -> list[dict[str, object]] | None:
    """Accept only the strictly customer-safe persisted citation shape.

    `CustomerCitation` forbids extra fields, so a malformed list or a staff
    projection containing chunk IDs/internal URLs makes the complete answer
    ineligible for SSE rather than silently leaking or partially rendering it.
    """
    raw_citations = payload.get("citations")
    if not isinstance(raw_citations, list):
        return None
    try:
        if not all(isinstance(citation, dict) for citation in raw_citations):
            return None
        return [
            dict(CustomerCitation.model_validate(citation).model_dump(mode="json"))
            for citation in raw_citations
        ]
    except ValidationError:
        return None


def _approved_segments(payload: dict[str, object]) -> list[str] | None:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        return None
    if not all(isinstance(segment, str) and segment.strip() for segment in raw_segments):
        return None
    return list(raw_segments)


def _cursor_sort_key(cursor: str) -> tuple[int, int, int]:
    """Accept legacy integer cursors while keeping durable segment ordering."""
    if cursor.isdecimal():
        # Older clients use message sequence.  Treat it as the end of that
        # message, preserving their original after={sequence} contract.
        return int(cursor), 9, 0
    parts = cursor.split(":")
    if len(parts) != 3 or not parts[0].isdecimal() or not parts[2].isdecimal():
        return 0, 9, 0
    category = {"v": 0, "s": 1, "e": 2, "t": -1}.get(parts[1])
    if category is None:
        return 0, 9, 0
    return int(parts[0]), category, int(parts[2])


def _channel(session_id: UUID) -> str:
    return f"chat:sse:{session_id}"


def _encode(event: ChatSSEEvent) -> str:
    return (
        f"id: {event.cursor}\n"
        f"event: {event.event}\n"
        f"data: {json.dumps(event.data, separators=(',', ':'))}\n\n"
    )
