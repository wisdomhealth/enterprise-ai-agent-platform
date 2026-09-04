from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID, uuid5

import tiktoken

from app.modules.knowledge.parsers import ParsedSection


class Tokenizer(Protocol):
    def encode(self, value: str) -> list[object]: ...

    def decode(self, tokens: list[object]) -> str: ...


@dataclass(frozen=True, slots=True)
class Chunk:
    id: UUID
    ordinal: int
    text: str
    page_number: int | None
    section: str | None
    token_count: int
    metadata: dict[str, object]


class DeterministicChunker:
    target_tokens = 700
    overlap_tokens = 100

    def __init__(self, tokenizer: Tokenizer | None = None) -> None:
        self._tokenizer: Tokenizer = (
            tokenizer
            if tokenizer is not None
            else cast(Tokenizer, tiktoken.encoding_for_model("text-embedding-3-small"))
        )

    def chunk(self, *, document_version_id: UUID, sections: list[ParsedSection]) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in sections:
            tokens = self._tokenizer.encode(section.text)
            start = 0
            while start < len(tokens):
                end = min(start + self.target_tokens, len(tokens))
                ordinal = len(chunks)
                chunk_tokens = tokens[start:end]
                chunks.append(
                    Chunk(
                        id=uuid5(document_version_id, f"chunk:{ordinal}"),
                        ordinal=ordinal,
                        text=self._tokenizer.decode(chunk_tokens),
                        page_number=section.page_number,
                        section=section.section,
                        token_count=len(chunk_tokens),
                        metadata={"chunking_version": "v1"},
                    )
                )
                if end == len(tokens):
                    break
                start = end - self.overlap_tokens
        return chunks
