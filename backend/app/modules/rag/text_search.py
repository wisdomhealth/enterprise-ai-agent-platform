from typing import cast
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.dependencies import Principal
from app.modules.knowledge.models import Document, DocumentChunk, DocumentVersion
from app.modules.rag.types import RetrievedChunk
from app.modules.rag.vector_search import _authorized_chunks_query, _candidate


class TextCandidateSource:
    """PostgreSQL full-text branch sharing the same pre-ranking authorization scope."""

    def __init__(self, db_session: AsyncSession | async_sessionmaker[AsyncSession]):
        self._db_session = db_session

    @property
    def bound_session(self) -> AsyncSession | None:
        """Expose session affinity so HybridRetriever can reject unsafe sharing."""
        return self._db_session if isinstance(self._db_session, AsyncSession) else None

    async def search(
        self, principal: Principal, knowledge_base_id: UUID, query: str, limit: int
    ) -> list[RetrievedChunk]:
        if limit < 1:
            return []
        if isinstance(self._db_session, AsyncSession):
            return await self._search_with_session(
                self._db_session, principal, knowledge_base_id, query, limit
            )
        async with self._db_session() as db_session:
            return await self._search_with_session(
                db_session, principal, knowledge_base_id, query, limit
            )

    async def _search_with_session(
        self,
        db_session: AsyncSession,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
    ) -> list[RetrievedChunk]:
        tsquery = func.plainto_tsquery("english", query)
        rows = await db_session.execute(
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
