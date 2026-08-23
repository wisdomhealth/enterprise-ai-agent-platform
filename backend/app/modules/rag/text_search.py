from typing import cast
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.dependencies import Principal
from app.modules.knowledge.models import Document, DocumentChunk, DocumentVersion
from app.modules.rag.types import RetrievedChunk
from app.modules.rag.vector_search import _authorized_chunks_query, _candidate


class TextCandidateSource:
    """PostgreSQL full-text branch sharing the same pre-ranking authorization scope."""

    def __init__(self, db_session: AsyncSession):
        self._db_session = db_session

    async def search(
        self, principal: Principal, knowledge_base_id: UUID, query: str, limit: int
    ) -> list[RetrievedChunk]:
        if limit < 1:
            return []
        tsquery = func.plainto_tsquery("english", query)
        rows = await self._db_session.execute(
            _authorized_chunks_query(principal, knowledge_base_id)
            .where(DocumentChunk.search_vector.op("@@")(tsquery))
            .order_by(
                func.ts_rank_cd(DocumentChunk.search_vector, tsquery).desc(), DocumentChunk.id
            )
            .limit(limit)
        )
        return [
            _candidate(cast(tuple[DocumentChunk, DocumentVersion, Document], tuple(row)))
            for row in rows.all()
        ]
