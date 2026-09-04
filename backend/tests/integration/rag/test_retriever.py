import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
)
from app.modules.rag.embeddings import EmbeddingPublicationService
from app.modules.rag.retriever import HybridRetriever
from app.modules.rag.types import RetrievedChunk


class FakeEmbeddingProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 1536 for _ in texts]


@pytest.mark.asyncio
async def test_embedding_publication_switches_current_version_only_after_every_chunk_is_valid(
    db_session,
) -> None:
    from app.modules.identity.models import Organization
    from app.modules.knowledge.models import DriveSource, KnowledgeBase

    organization = Organization(name=f"embedding {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="root",
        connection_identity="reader@example.test",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id="document",
        title="Policy",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    version = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.PROCESSING,
        content_sha256="a" * 64,
    )
    db_session.add(version)
    await db_session.flush()
    db_session.add_all(
        [
            DocumentChunk(
                id=uuid4(), document_version_id=version.id, ordinal=index, text=f"policy {index}",
                page_number=None, section=None, token_count=2, metadata_={}
            )
            for index in range(2)
        ]
    )
    await db_session.flush()

    published = await EmbeddingPublicationService(db_session, FakeEmbeddingProvider()).publish(
        version.id
    )

    assert published.state is DocumentVersionState.RETRIEVABLE
    assert document.current_version_id == version.id
    from sqlalchemy import select

    chunks = list(
        (
            await db_session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
            )
        ).all()
    )
    assert all(chunk.embedding is not None for chunk in chunks)


@pytest.mark.asyncio
async def test_hybrid_retriever_runs_both_branches_and_fuses_stable_chunk_ids() -> None:
    class VectorSource:
        async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return [chunk("a"), chunk("b")]

    class TextSource:
        async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return [chunk("b"), chunk("c")]

    class Provider:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] * 1536]

    result = await HybridRetriever(VectorSource(), TextSource(), Provider()).retrieve(
        object(), uuid4(), "policy", 10
    )

    assert [item.stable_id for item in result] == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_hybrid_retriever_uses_independent_postgresql_sessions_for_parallel_branches(
) -> None:
    from app.core.database import async_sessionmaker, engine
    from app.modules.authorization.models import ResourceGrant
    from app.modules.identity.dependencies import Principal
    from app.modules.identity.models import (
        Organization,
        StaffSession,
        StaffUser,
        UserRole,
        UserStatus,
    )
    from app.modules.knowledge.models import DriveSource, KnowledgeBase

    async with async_sessionmaker() as db_session:
        organization = Organization(name=f"parallel hybrid {uuid4()}")
        db_session.add(organization)
        await db_session.flush()
        knowledge_base = KnowledgeBase(organization_id=organization.id)
        db_session.add(knowledge_base)
        await db_session.flush()
        source = DriveSource(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            root_folder_id="root",
            connection_identity="reader@example.test",
        )
        db_session.add(source)
        user = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"parallel-{uuid4()}",
            email="member@example.test",
            role=UserRole.MEMBER,
            status=UserStatus.ACTIVE,
        )
        db_session.add(user)
        await db_session.flush()
        session = StaffSession(
            user_id=user.id,
            csrf_hash="test",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        db_session.add(session)
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=user.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.read"],
            )
        )
        document = Document(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            source_id=source.id,
            external_id=str(uuid4()),
            title="Policy",
            mime_type="application/pdf",
        )
        db_session.add(document)
        await db_session.flush()
        version = DocumentVersion(
            document_id=document.id,
            state=DocumentVersionState.RETRIEVABLE,
            content_sha256=uuid4().hex + uuid4().hex,
        )
        db_session.add(version)
        await db_session.flush()
        document.current_version_id = version.id
        db_session.add(
            DocumentChunk(
                id=uuid4(),
                document_version_id=version.id,
                ordinal=0,
                text="policy refund",
                page_number=1,
                section="Policy",
                token_count=2,
                metadata_={},
                embedding=[1.0] * 1536,
            )
        )
        await db_session.commit()

        principal = Principal(user.id, organization.id, user.email, user.role, session.id, "test")
        document_id = document.id
        knowledge_base_id = knowledge_base.id
        organization_id = organization.id

    try:
        result = await HybridRetriever.from_session_factory(
            async_sessionmaker,
            FakeEmbeddingProvider(),
        ).retrieve(principal, knowledge_base_id, "policy", 10)

        assert [item.document_id for item in result] == [document_id]
    finally:
        async with async_sessionmaker() as cleanup_session:
            await cleanup_session.execute(
                delete(Organization).where(Organization.id == organization_id)
            )
            await cleanup_session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_hybrid_branches_overlap_clean_up_and_fuse_only_authorized_results(
) -> None:
    """Exercise real pgvector/FTS session ownership, lifecycle, and eligibility."""
    from app.core.database import async_sessionmaker, engine
    from app.modules.authorization.models import ResourceGrant
    from app.modules.identity.dependencies import Principal
    from app.modules.identity.models import (
        Organization,
        StaffSession,
        StaffUser,
        UserRole,
        UserStatus,
    )
    from app.modules.knowledge.models import DriveSource, DriveSourceStatus, KnowledgeBase
    from app.modules.rag.text_search import TextCandidateSource as PostgreSQLTextCandidateSource
    from app.modules.rag.vector_search import (
        VectorCandidateSource as PostgreSQLVectorCandidateSource,
    )

    class OverlappingVectorSource(PostgreSQLVectorCandidateSource):
        def __init__(self, started, peer_started, release, backend_pids) -> None:  # type: ignore[no-untyped-def]
            super().__init__(async_sessionmaker)
            self.started = started
            self.peer_started = peer_started
            self.release = release
            self.backend_pids = backend_pids
            self.results: list[list[RetrievedChunk]] = []

        async def _search_with_session(self, db_session, *args, **kwargs):  # type: ignore[no-untyped-def]
            backend_pid = await db_session.scalar(select(func.pg_backend_pid()))
            assert isinstance(backend_pid, int)
            self.backend_pids.add(backend_pid)
            self.started.set()
            await asyncio.wait_for(self.peer_started.wait(), timeout=1)
            await self.release.wait()
            result = await super()._search_with_session(db_session, *args, **kwargs)
            self.results.append(result)
            return result

    class OverlappingTextSource(PostgreSQLTextCandidateSource):
        def __init__(self, started, peer_started, release, backend_pids) -> None:  # type: ignore[no-untyped-def]
            super().__init__(async_sessionmaker)
            self.started = started
            self.peer_started = peer_started
            self.release = release
            self.backend_pids = backend_pids
            self.results: list[list[RetrievedChunk]] = []

        async def _search_with_session(self, db_session, *args, **kwargs):  # type: ignore[no-untyped-def]
            backend_pid = await db_session.scalar(select(func.pg_backend_pid()))
            assert isinstance(backend_pid, int)
            self.backend_pids.add(backend_pid)
            self.started.set()
            await asyncio.wait_for(self.peer_started.wait(), timeout=1)
            await self.release.wait()
            result = await super()._search_with_session(db_session, *args, **kwargs)
            self.results.append(result)
            return result

    async with async_sessionmaker() as db_session:
        organization = Organization(name=f"parallel lifecycle {uuid4()}")
        db_session.add(organization)
        await db_session.flush()
        knowledge_base = KnowledgeBase(organization_id=organization.id)
        db_session.add(knowledge_base)
        await db_session.flush()
        source = DriveSource(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            root_folder_id="root",
            connection_identity="reader@example.test",
        )
        db_session.add(source)
        user = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"parallel-lifecycle-{uuid4()}",
            email="member@example.test",
            role=UserRole.MEMBER,
            status=UserStatus.ACTIVE,
        )
        db_session.add(user)
        await db_session.flush()
        staff_session = StaffSession(
            user_id=user.id,
            csrf_hash="test",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        db_session.add(staff_session)
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=user.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.read"],
            )
        )
        document = Document(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            source_id=source.id,
            external_id=str(uuid4()),
            title="Policy",
            mime_type="application/pdf",
        )
        db_session.add(document)
        await db_session.flush()
        version = DocumentVersion(
            document_id=document.id,
            state=DocumentVersionState.RETRIEVABLE,
            content_sha256=uuid4().hex + uuid4().hex,
        )
        db_session.add(version)
        await db_session.flush()
        document.current_version_id = version.id
        db_session.add(
            DocumentChunk(
                id=uuid4(),
                document_version_id=version.id,
                ordinal=0,
                text="policy refund",
                page_number=1,
                section="Policy",
                token_count=2,
                metadata_={},
                embedding=[1.0] * 1536,
            )
        )
        await db_session.commit()

        principal = Principal(
            user.id, organization.id, user.email, user.role, staff_session.id, "test"
        )
        document_id = document.id
        source_id = source.id
        knowledge_base_id = knowledge_base.id
        organization_id = organization.id

    try:
        baseline_checked_out = engine.pool.checkedout()
        vector_started = asyncio.Event()
        text_started = asyncio.Event()
        release_branches = asyncio.Event()
        backend_pids: set[int] = set()
        vector_source = OverlappingVectorSource(
            vector_started, text_started, release_branches, backend_pids
        )
        text_source = OverlappingTextSource(
            text_started, vector_started, release_branches, backend_pids
        )
        retriever = HybridRetriever(vector_source, text_source, FakeEmbeddingProvider())

        retrieval = asyncio.create_task(
            retriever.retrieve(principal, knowledge_base_id, "policy", 10)
        )
        await asyncio.wait_for(vector_started.wait(), timeout=1)
        await asyncio.wait_for(text_started.wait(), timeout=1)
        assert len(backend_pids) == 2
        release_branches.set()
        result = await retrieval
        assert [item.document_id for item in result] == [document_id]
        assert vector_source.results == [result]
        assert text_source.results == [result]
        await asyncio.sleep(0)
        assert engine.pool.checkedout() == baseline_checked_out

        repeated_result = await retriever.retrieve(principal, knowledge_base_id, "policy", 10)
        assert [item.stable_id for item in repeated_result] == [item.stable_id for item in result]
        assert engine.pool.checkedout() == baseline_checked_out

        async with async_sessionmaker() as revocation_session:
            revoked_source = await revocation_session.get(DriveSource, source_id)
            assert revoked_source is not None
            revoked_source.status = DriveSourceStatus.DISABLED
            await revocation_session.commit()

        denied_result = await retriever.retrieve(principal, knowledge_base_id, "policy", 10)
        assert denied_result == []
        assert vector_source.results[-1] == []
        assert text_source.results[-1] == []
        assert engine.pool.checkedout() == baseline_checked_out

        vector_finished = asyncio.Event()

        class CompletingVectorSource(PostgreSQLVectorCandidateSource):
            async def _search_with_session(self, db_session, *args, **kwargs):  # type: ignore[no-untyped-def]
                try:
                    await db_session.scalar(select(func.pg_backend_pid()))
                    return await super()._search_with_session(db_session, *args, **kwargs)
                finally:
                    vector_finished.set()

        class FailingTextSource(PostgreSQLTextCandidateSource):
            async def _search_with_session(self, db_session, *args, **kwargs):  # type: ignore[no-untyped-def]
                await db_session.scalar(select(func.pg_backend_pid()))
                raise RuntimeError("forced FTS branch failure")

        failing_retriever = HybridRetriever(
            CompletingVectorSource(async_sessionmaker),
            FailingTextSource(async_sessionmaker),
            FakeEmbeddingProvider(),
        )
        with pytest.raises(RuntimeError, match="forced FTS branch failure"):
            await failing_retriever.retrieve(principal, knowledge_base_id, "policy", 10)
        await asyncio.wait_for(vector_finished.wait(), timeout=1)
        await asyncio.sleep(0)
        assert engine.pool.checkedout() == baseline_checked_out

        vector_started = asyncio.Event()
        text_started = asyncio.Event()
        never_release = asyncio.Event()
        cancelled_vector = OverlappingVectorSource(
            vector_started, text_started, never_release, set()
        )
        cancelled_text = OverlappingTextSource(
            text_started, vector_started, never_release, set()
        )
        cancellation = asyncio.create_task(
            HybridRetriever(
                cancelled_vector, cancelled_text, FakeEmbeddingProvider()
            ).retrieve(principal, knowledge_base_id, "policy", 10)
        )
        await asyncio.wait_for(vector_started.wait(), timeout=1)
        await asyncio.wait_for(text_started.wait(), timeout=1)
        cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancellation
        await asyncio.sleep(0)
        assert engine.pool.checkedout() == baseline_checked_out
    finally:
        async with async_sessionmaker() as cleanup_session:
            await cleanup_session.execute(
                delete(Organization).where(Organization.id == organization_id)
            )
            await cleanup_session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_retriever_rejects_shared_postgresql_session(db_session) -> None:  # type: ignore[no-untyped-def]
    from app.modules.rag.text_search import TextCandidateSource
    from app.modules.rag.vector_search import VectorCandidateSource

    retriever = HybridRetriever(
        VectorCandidateSource(db_session),
        TextCandidateSource(db_session),
        FakeEmbeddingProvider(),
    )

    with pytest.raises(RuntimeError, match="independently scoped database sessions"):
        await retriever.retrieve(object(), uuid4(), "policy", 10)


def chunk(stable_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid4(), stable_id=stable_id, document_version_id=uuid4(), document_id=uuid4(),
        organization_id=uuid4(), knowledge_base_id=uuid4(), ordinal=0, text=stable_id,
        page_number=None, section=None, resource_authorized=True,
    )
