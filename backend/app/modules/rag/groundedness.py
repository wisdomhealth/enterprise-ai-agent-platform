import re
from uuid import UUID

from app.modules.identity.dependencies import Principal
from app.modules.rag.llm import GeneratedAnswer
from app.modules.rag.types import RetrievedChunk

_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class GroundednessError(ValueError):
    pass


class CitationValidator:
    """Reject output unless every cited source is from this authorized retrieval."""

    def validate(
        self,
        generation: GeneratedAnswer,
        retrieved_chunks: list[RetrievedChunk],
        principal: Principal,
        knowledge_base_id: UUID,
    ) -> list[RetrievedChunk]:
        indexed = {chunk.chunk_id: chunk for chunk in retrieved_chunks}
        answer_sentences = _sentences(generation.text)
        if not answer_sentences:
            raise GroundednessError("answer omitted content")
        if not generation.claims:
            raise GroundednessError("non-empty answer omitted atomic claims")
        claim_texts = {_normalized(claim.text) for claim in generation.claims}
        if any(_normalized(sentence) not in claim_texts for sentence in answer_sentences):
            raise GroundednessError("answer sentence is not covered by an atomic claim")
        cited_ids: list[UUID] = []
        for claim in generation.claims:
            if not claim.citation_ids:
                raise GroundednessError("claim omitted supporting citations")
            supporting_chunks: list[RetrievedChunk] = []
            for citation_id in claim.citation_ids:
                chunk = indexed.get(citation_id)
                if chunk is None:
                    raise GroundednessError("claim cites a chunk outside this retrieval")
                self._require_authorized(chunk, principal, knowledge_base_id)
                supporting_chunks.append(chunk)
                if citation_id not in cited_ids:
                    cited_ids.append(citation_id)
            if not _claim_supported(claim.text, supporting_chunks):
                raise GroundednessError("claim lacks textual source support")
        return [indexed[citation_id] for citation_id in cited_ids]

    @staticmethod
    def _require_authorized(
        chunk: RetrievedChunk, principal: Principal, knowledge_base_id: UUID
    ) -> None:
        if (
            not chunk.resource_authorized
            or not chunk.retrieval_eligible
            or chunk.organization_id != principal.organization_id
            or chunk.knowledge_base_id != knowledge_base_id
        ):
            raise GroundednessError("claim cites unauthorized or revoked source")


def _claim_supported(claim: str, chunks: list[RetrievedChunk]) -> bool:
    """Accept only a complete, directly stated claim in one cited evidence sentence.

    Token-set overlap cannot distinguish a statement from its negation, qualifier, or a
    sentence assembled from unrelated evidence. This intentionally narrow contract fails
    closed for paraphrases and other semantic uncertainty until a stronger independent
    entailment check is available.
    """
    normalized_claim = _normalized(claim)
    if not normalized_claim:
        return False
    return any(
        normalized_claim == _normalized(sentence)
        for chunk in chunks
        for sentence in _sentences(chunk.text)
    )


def _sentences(text: str) -> list[str]:
    return [sentence for sentence in _SENTENCE.split(text.strip()) if sentence]


def _normalized(text: str) -> str:
    return " ".join(_WORD.findall(text.casefold()))
