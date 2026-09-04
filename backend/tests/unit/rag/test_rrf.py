from uuid import uuid4

from app.modules.rag.rrf import reciprocal_rank_fusion
from app.modules.rag.types import RetrievedChunk


def candidate(chunk_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid4(),
        stable_id=chunk_id,
        document_version_id=uuid4(),
        document_id=uuid4(),
        organization_id=uuid4(),
        knowledge_base_id=uuid4(),
        ordinal=0,
        text=chunk_id,
        page_number=None,
        section=None,
        resource_authorized=True,
    )


def test_rrf_fuses_without_comparing_provider_score_scales() -> None:
    vector = [candidate("a"), candidate("b")]
    text = [candidate("b"), candidate("c")]

    fused = reciprocal_rank_fusion([vector, text], k=60)

    assert [item.stable_id for item in fused] == ["b", "a", "c"]
