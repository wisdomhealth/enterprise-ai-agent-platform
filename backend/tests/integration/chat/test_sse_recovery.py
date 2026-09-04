from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.chat.models import ChatActor, ChatMessage, ChatMessageStatus, ChatSession
from app.modules.chat.sse import PostgresSSEService
from app.modules.chat.tokens import ChatTokenService
from app.modules.identity.dependencies import get_db_session
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent


async def _session(db_session: AsyncSession) -> ChatSession:
    organization = Organization(name=f"SSE recovery {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{uuid4().hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
    )
    db_session.add(session)
    await db_session.flush()
    return session


@pytest.mark.asyncio
async def test_reconnect_replays_from_postgres_not_redis(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    customer = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.CUSTOMER,
        body="Question",
        status=ChatMessageStatus.PERSISTED,
    )
    first = ChatMessage(
        session_id=session.id,
        sequence=2,
        actor=ChatActor.AI,
        body="First validated answer.",
        status=ChatMessageStatus.PERSISTED,
    )
    second = ChatMessage(
        session_id=session.id,
        sequence=3,
        actor=ChatActor.AI,
        body="Second validated answer.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add_all([customer, first, second])
    await db_session.flush()
    db_session.add_all(
        [
            OutboxEvent(
                event_type="chat.answer.validated",
                aggregate_type="chat_session",
                aggregate_id=session.id,
                payload={
                    "sequence": first.sequence,
                    "message_id": str(first.id),
                    "segments": [first.body],
                    "citations": [],
                },
            ),
            OutboxEvent(
                event_type="chat.answer.validated",
                aggregate_type="chat_session",
                aggregate_id=session.id,
                payload={
                    "sequence": second.sequence,
                    "message_id": str(second.id),
                    "segments": [second.body],
                    "citations": [],
                },
            ),
        ]
    )
    await db_session.commit()

    events = await PostgresSSEService(db_session).events_after(session.id, after_sequence=1)

    assert [event.sequence for event in events if event.event == "message.validated"] == [2, 3]


@pytest.mark.asyncio
async def test_sse_hides_ai_and_system_messages_without_safe_durable_provenance(
    db_session: AsyncSession,
) -> None:
    """A bare persisted body is never a groundedness or safe-error proof."""
    session = await _session(db_session)
    db_session.add_all(
        [
            ChatMessage(
                session_id=session.id,
                sequence=1,
                actor=ChatActor.AI,
                body="Ungrounded model output must stay hidden.",
                status=ChatMessageStatus.PERSISTED,
            ),
            ChatMessage(
                session_id=session.id,
                sequence=2,
                actor=ChatActor.SYSTEM,
                body="An unproven safe error must stay hidden too.",
                status=ChatMessageStatus.PERSISTED,
            ),
        ]
    )
    await db_session.commit()

    events = await PostgresSSEService(db_session).events_after(session.id, after_cursor="0")

    assert events == []


@pytest.mark.asyncio
async def test_sse_uses_persisted_validated_segments(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.AI,
        body="Provider text that must not be re-segmented.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.validated",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": 1,
                "message_id": str(message.id),
                "segments": ["Persisted validated sentence."],
                "citations": [],
            },
        )
    )
    await db_session.commit()

    events = await PostgresSSEService(db_session).events_after(session.id, after_sequence=0)

    assert events[0].event == "message.validated"
    assert "body" not in events[0].data
    assert events[0].data["segment_count"] == 1
    assert events[1].event == "message.segment"
    assert events[1].data["text"] == "Persisted validated sentence."


@pytest.mark.asyncio
async def test_sse_reconnect_replays_only_missing_persisted_segments(
    db_session: AsyncSession,
) -> None:
    """A segment cursor must resume within one validated answer, not skip it."""
    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=2,
        actor=ChatActor.AI,
        body="First validated sentence. Second validated sentence.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.validated",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": 2,
                "message_id": str(message.id),
                "segments": ["First validated sentence.", "Second validated sentence."],
                "citations": [],
            },
        )
    )
    await db_session.commit()

    events = await PostgresSSEService(db_session).events_after(
        session.id, after_cursor="2:s:0"
    )

    assert [(event.event, event.data.get("index")) for event in events] == [
        ("message.segment", 1)
    ]
    assert events[0].cursor == "2:s:1"
    assert events[0].data["text"] == "Second validated sentence."


def test_configured_redis_is_available_for_ephemeral_sse_hints() -> None:
    application = create_app(
        Settings(
            SESSION_SECRET="task-fourteen-session-secret",
            REDIS_URL="redis://127.0.0.1:6379/15",
        )
    )

    assert hasattr(application.state, "chat_sse_redis")


@pytest.mark.asyncio
async def test_sse_endpoint_replays_postgres_after_sequence(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    issued = ChatTokenService().issue(session_id=session.id)
    from app.modules.chat.models import ChatSessionCredential

    db_session.add(
        ChatSessionCredential(
            session_id=session.id,
            token_hash=issued.token_hash,
            expires_at=issued.expires_at,
        )
    )
    customer = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.CUSTOMER,
        body="Question",
        status=ChatMessageStatus.PERSISTED,
    )
    answer = ChatMessage(
        session_id=session.id,
        sequence=2,
        actor=ChatActor.AI,
        body="Validated response.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add_all([customer, answer])
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.validated",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": answer.sequence,
                "message_id": str(answer.id),
                "segments": [answer.body],
                "citations": [],
            },
        )
    )
    await db_session.commit()
    application: FastAPI = create_app(
        Settings(
            SESSION_SECRET="task-fourteen-session-secret",
            REDIS_URL=None,
        )
    )

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = override_db_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://testserver"
    ) as client:
        response = await client.get(
            f"/api/v1/public/chat/sessions/{session.id}/events?after=1",
            headers={"Authorization": f"Bearer {issued.value}"},
        )

    assert response.status_code == 200
    assert "id: 2" in response.text
    assert "event: message.validated" in response.text


@pytest.mark.asyncio
async def test_sse_stream_reads_durable_session_state(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    await db_session.commit()

    frames = [
        frame
        async for frame in PostgresSSEService(db_session).stream(session.id, after_sequence=0)
    ]

    assert "event: session.state" in frames[0]


@pytest.mark.asyncio
async def test_persisted_refusal_is_exposed_as_a_safe_error(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.AI,
        body="I don't know based on the available information.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.refused",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": 1,
                "message_id": str(message.id),
                "segments": [message.body],
                "citations": [],
                "refused": True,
                "handoff_recommended": True,
                "safe_body": message.body,
            },
        )
    )
    await db_session.commit()

    event = (await PostgresSSEService(db_session).events_after(session.id, after_sequence=0))[0]

    assert event.event == "error.safe"
    assert event.data["body"] == message.body
    assert event.data["handoff_recommended"] is True


@pytest.mark.asyncio
async def test_sse_hides_refusal_when_outbox_safe_body_does_not_bind_message(
    db_session: AsyncSession,
) -> None:
    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.AI,
        body="The persisted refusal body.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.refused",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": message.sequence,
                "message_id": str(message.id),
                "segments": [message.body],
                "citations": [],
                "refused": True,
                "handoff_recommended": True,
                "safe_body": "A different safe body must not be exposed.",
            },
        )
    )
    await db_session.commit()

    events = await PostgresSSEService(db_session).events_after(session.id, after_cursor="0")

    assert events == []


@pytest.mark.asyncio
async def test_sse_hides_safe_error_when_outbox_safe_body_does_not_bind_message(
    db_session: AsyncSession,
) -> None:
    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.SYSTEM,
        body="The persisted safe error body.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.safe_error",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": message.sequence,
                "message_id": str(message.id),
                "code": "CHAT_ANSWER_UNAVAILABLE",
                "handoff_recommended": True,
                "safe_body": "A different safe body must not be exposed.",
            },
        )
    )
    await db_session.commit()

    events = await PostgresSSEService(db_session).events_after(session.id, after_cursor="0")

    assert events == []


@pytest.mark.asyncio
async def test_sse_hides_validated_answer_with_internal_citation_metadata(
    db_session: AsyncSession,
) -> None:
    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.AI,
        body="A validated answer with an unsafe citation payload.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.validated",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": message.sequence,
                "message_id": str(message.id),
                "segments": [message.body],
                "citations": [
                    {
                        "title": "Customer-safe title",
                        "section": "Overview",
                        "page_number": 1,
                        "internal_drive_link": "https://drive.example.internal/private",
                    }
                ],
            },
        )
    )
    await db_session.commit()

    events = await PostgresSSEService(db_session).events_after(session.id, after_cursor="0")

    assert events == []


@pytest.mark.asyncio
async def test_sse_emits_safe_error_from_bound_outbox_safe_body(
    db_session: AsyncSession,
) -> None:
    session = await _session(db_session)
    message = ChatMessage(
        session_id=session.id,
        sequence=1,
        actor=ChatActor.SYSTEM,
        body="The approved customer-safe error.",
        status=ChatMessageStatus.PERSISTED,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type="chat.answer.safe_error",
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "sequence": message.sequence,
                "message_id": str(message.id),
                "code": "CHAT_ANSWER_UNAVAILABLE",
                "handoff_recommended": True,
                "safe_body": message.body,
            },
        )
    )
    await db_session.commit()

    event = (await PostgresSSEService(db_session).events_after(session.id, after_cursor="0"))[0]

    assert event.event == "error.safe"
    assert event.data["body"] == "The approved customer-safe error."
