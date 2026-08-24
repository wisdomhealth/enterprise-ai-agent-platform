from typing import Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session
from app.modules.knowledge.models import KnowledgeBase
from app.modules.rag.types import AnswerAudience, ValidatedAnswer

router = APIRouter(prefix="/api/v1/staff/knowledge", tags=["staff-assist"])


class StaffAnswerService(Protocol):
    async def answer(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> ValidatedAnswer: ...


class StaffAssistRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)


async def _staff_knowledge_base(
    principal: Principal = Depends(require_staff_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> UUID:
    knowledge_base_id = await db_session.scalar(
        select(KnowledgeBase.id).where(KnowledgeBase.organization_id == principal.organization_id)
    )
    if knowledge_base_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowledge base not found",
        )
    return knowledge_base_id


def _grounded_answer_service(request: Request) -> StaffAnswerService:
    service = getattr(request.app.state, "grounded_answer_service", None)
    if service is None or not callable(getattr(service, "answer", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Staff Assist is not configured",
        )
    return cast(StaffAnswerService, service)


@router.post("/search", response_model=ValidatedAnswer)
async def staff_assist_search(
    payload: StaffAssistRequest,
    principal: Principal = Depends(require_staff_session),
    knowledge_base_id: UUID = Depends(_staff_knowledge_base),
    answer_service: StaffAnswerService = Depends(_grounded_answer_service),
) -> ValidatedAnswer:
    """Read-only employee reference lookup; it creates no events or workflow state."""
    return await answer_service.answer(
        principal,
        knowledge_base_id,
        payload.question,
        AnswerAudience.STAFF,
    )
