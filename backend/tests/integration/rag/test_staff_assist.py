from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.modules.identity.dependencies import Principal, require_staff_session
from app.modules.identity.models import UserRole
from app.modules.rag.router import _staff_knowledge_base, router
from app.modules.rag.types import AnswerAudience, ClaimSupport, SourceCitation, ValidatedAnswer


class FakeStaffAnswerService:
    def __init__(self, citation: SourceCitation) -> None:
        self.citation = citation
        self.calls: list[tuple[Principal, UUID, str, AnswerAudience]] = []

    async def answer(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        question: str,
        audience: AnswerAudience,
    ) -> ValidatedAnswer:
        self.calls.append((principal, knowledge_base_id, question, audience))
        return ValidatedAnswer(
            text="Refunds take five business days.",
            claims=[
                ClaimSupport(
                    text="Refunds take five business days.",
                    citation_ids=[self.citation.chunk_id],
                )
            ],
            citations=[self.citation],
            segments=["Refunds take five business days."],
            refused=False,
            model="fake-staff-model",
            prompt_version="grounded-answer-v1",
            latency_ms=1,
            input_tokens=3,
            output_tokens=4,
            estimated_cost=0.0,
        )


@pytest.mark.asyncio
async def test_staff_assist_returns_internal_sources_without_side_effects() -> None:
    principal = Principal(
        uuid4(), uuid4(), "reviewer@example.test", UserRole.REVIEWER, uuid4(), "csrf"
    )
    knowledge_base_id = uuid4()
    fake_service = FakeStaffAnswerService(
        SourceCitation(
            chunk_id=uuid4(),
            document_version_id=uuid4(),
            title="Refund policy",
            section="Eligibility",
            page_number=2,
            internal_drive_link="https://drive.google.com/private",
        )
    )
    app = FastAPI()
    app.state.grounded_answer_service = fake_service
    app.include_router(router)
    app.dependency_overrides[require_staff_session] = lambda: principal
    app.dependency_overrides[_staff_knowledge_base] = lambda: knowledge_base_id
    outbox_events: list[str] = []

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            before = len(outbox_events)
            response = await client.post(
                "/api/v1/staff/knowledge/search",
                json={"question": "What is the refund policy?"},
            )

        assert response.status_code == 200
        assert "chunk_id" in response.json()["citations"][0]
        assert response.json()["citations"][0]["internal_drive_link"] == "https://drive.google.com/private"
        assert len(outbox_events) == before
        assert fake_service.calls[0][3] is AnswerAudience.STAFF
    finally:
        app.dependency_overrides.clear()
