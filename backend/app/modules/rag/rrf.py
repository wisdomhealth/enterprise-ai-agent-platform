from collections.abc import Sequence

from app.modules.rag.types import RetrievedChunk


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievedChunk]], *, k: int = 60
) -> list[RetrievedChunk]:
    """Fuse independently-ranked candidates without comparing provider score scales."""
    if k < 1:
        raise ValueError("k must be positive")
    scores: dict[str, float] = {}
    candidates: dict[str, RetrievedChunk] = {}
    first_seen: dict[str, int] = {}
    encounter = 0
    for ranking in rankings:
        for rank, candidate in enumerate(ranking, start=1):
            stable_id = candidate.stable_id
            scores[stable_id] = scores.get(stable_id, 0.0) + 1.0 / (k + rank)
            if stable_id not in candidates:
                candidates[stable_id] = candidate
                first_seen[stable_id] = encounter
                encounter += 1
    return sorted(
        candidates.values(),
        key=lambda candidate: (-scores[candidate.stable_id], first_seen[candidate.stable_id]),
    )
