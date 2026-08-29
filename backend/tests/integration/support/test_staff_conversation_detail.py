from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.models import (
    ChatActor,
    ChatMessage,
    ChatSession,
    ChatSessionCredential,
)
from app.modules.chat.tokens import ChatTokenService
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent
from app.modules.support.models import HandoffTrigger
from app.modules.support.service import SupportService


@dataclass(frozen=True, slots=True)
class DetailCase:
    handoff_id: UUID
    session: ChatSession
    ai_message: ChatMessage
    principal: Principal
    public_token: str


def _source_citation() -> dict[str, object]:
    return {
        "chunk_id": str(uuid4()),
        "document_version_id": str(uuid4()),
        "title": "Support policy",
        "section": "Response times",
        "page_number": 2,
        "internal_drive_link": "https://drive.google.com/internal-policy",
    }


async def _case(
    db_session: AsyncSession,
    *,
    grant_access: bool = True,
    staff_citations: object | None = None,
    bound_message_id: str | None = None,
    bound_sequence: int | None = None,
    event_type: str = "chat.answer.validated",
) -> DetailCase:
    organization = Organization(name=f"Support detail {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"public-{uuid4().hex}"
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        customer_name="Ada",
        customer_email="ada@example.test",
    )
    db_session.add(session)
    await db_session.flush()
    customer = ChatMessage(
        session_id=session.id, sequence=1, actor=ChatActor.CUSTOMER, body="Need help"
    )
    ai_message = ChatMessage(
        session_id=session.id, sequence=2, actor=ChatActor.AI, body="How can I help?"
    )
    db_session.add_all([customer, ai_message])
    await db_session.flush()
    db_session.add(
        OutboxEvent(
            event_type=event_type,
            aggregate_type="chat_session",
            aggregate_id=session.id,
            payload={
                "message_id": bound_message_id or str(ai_message.id),
                "sequence": bound_sequence if bound_sequence is not None else ai_message.sequence,
                "segments": [ai_message.body],
                "citations": [
                    {"title": "Support policy", "section": "Response times", "page_number": 2}
                ],
                "staff_citations": staff_citations
                if staff_citations is not None
                else [_source_citation()],
                "refused": False,
            },
        )
    )
    reviewer = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"support-{uuid4()}",
        email=f"reviewer-{uuid4()}@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(reviewer)
    await db_session.flush()
    if grant_access:
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=reviewer.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.review"],
            )
        )
    issued = ChatTokenService().issue(session_id=session.id)
    db_session.add(
        ChatSessionCredential(
            session_id=session.id,
            token_hash=issued.token_hash,
            expires_at=issued.expires_at,
        )
    )
    await db_session.flush()
    handoff = await SupportService(db_session).request_handoff(
        session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST
    )
    await db_session.commit()
    principal = Principal(
        reviewer.id, organization.id, reviewer.email, UserRole.REVIEWER, uuid4(), ""
    )
    return DetailCase(handoff.id, session, ai_message, principal, issued.value)


@asynccontextmanager
async def _client(
    db_session: AsyncSession, principal: Principal
) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(Settings(SESSION_SECRET="staff-detail-secret"))

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_staff() -> Principal:
        return principal

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_staff_session] = override_staff
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_staff_can_read_authorized_bound_internal_citations_without_public_leakage(
    db_session: AsyncSession,
) -> None:
    citation = _source_citation()
    case = await _case(db_session, staff_citations=[citation])

    async with _client(db_session, case.principal) as client:
        staff_response = await client.get(f"/api/v1/staff/support/{case.handoff_id}")
        public_response = await client.get(
            f"/api/v1/public/chat/sessions/{case.session.id}",
            headers={"Authorization": f"Bearer {case.public_token}"},
        )
        sse_response = await client.get(
            f"/api/v1/public/chat/sessions/{case.session.id}/events?after=0",
            headers={"Authorization": f"Bearer {case.public_token}"},
        )

    assert staff_response.status_code == 200
    payload = staff_response.json()
    assert payload["trigger"] == "CUSTOMER_REQUEST"
    assert payload["customer"] == {"name": "Ada", "email": "ada@example.test"}
    assert [(message["sequence"], message["body"]) for message in payload["messages"]] == [
        (1, "Need help"),
        (2, "How can I help?"),
    ]
    assert payload["messages"][1]["citations"] == [citation]
    assert public_response.status_code == 200
    assert "staff_citations" not in public_response.text
    assert "chunk_id" not in public_response.text
    assert "internal_drive_link" not in public_response.text
    assert sse_response.status_code == 200
    assert "staff_citations" not in sse_response.text
    assert "chunk_id" not in sse_response.text
    assert "internal_drive_link" not in sse_response.text


@pytest.mark.asyncio
async def test_staff_detail_denies_internal_citations_without_resource_authorization(
    db_session: AsyncSession,
) -> None:
    case = await _case(db_session, grant_access=False)

    async with _client(db_session, case.principal) as client:
        response = await client.get(f"/api/v1/staff/support/{case.handoff_id}")

    assert response.status_code == 403
    assert "internal_drive_link" not in response.text


@pytest.mark.asyncio
async def test_staff_detail_fails_closed_for_malformed_internal_citation_schema(
    db_session: AsyncSession,
) -> None:
    malformed = {**_source_citation(), "chunk_id": "not-a-uuid"}
    case = await _case(db_session, staff_citations=[malformed])

    async with _client(db_session, case.principal) as client:
        response = await client.get(f"/api/v1/staff/support/{case.handoff_id}")

    assert response.status_code == 200
    assert response.json()["messages"][1]["citations"] == []
    assert "not-a-uuid" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding", "event_type"),
    [
        ({"bound_message_id": str(uuid4())}, "chat.answer.validated"),
        ({"bound_sequence": 999}, "chat.answer.validated"),
        ({}, "support.handoff.queued"),
    ],
)
async def test_staff_detail_rejects_misbound_or_non_answer_citation_provenance(
    db_session: AsyncSession,
    binding: dict[str, object],
    event_type: str,
) -> None:
    case = await _case(db_session, event_type=event_type, **binding)  # type: ignore[arg-type]

    async with _client(db_session, case.principal) as client:
        response = await client.get(f"/api/v1/staff/support/{case.handoff_id}")

    assert response.status_code == 200
    assert response.json()["messages"][1]["citations"] == []
