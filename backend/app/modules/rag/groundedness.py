import re
from collections.abc import Iterable
from uuid import UUID

from app.modules.identity.dependencies import Principal
from app.modules.rag.llm import GeneratedAnswer
from app.modules.rag.types import RetrievedChunk

_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "with",
    }
)


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


def _claim_supported(claim: str, chunks: Iterable[RetrievedChunk]) -> bool:
    meaningful_words = {
        word
        for word in _WORD.findall(claim.casefold())
        if word not in _STOP_WORDS and len(word) > 1
    }
    if not meaningful_words:
        return False
    source_words = {word for chunk in chunks for word in _WORD.findall(chunk.text.casefold())}
    return meaningful_words.issubset(source_words)


def _sentences(text: str) -> list[str]:
    return [sentence for sentence in _SENTENCE.split(text.strip()) if sentence]


def _normalized(text: str) -> str:
    return " ".join(_WORD.findall(text.casefold()))
