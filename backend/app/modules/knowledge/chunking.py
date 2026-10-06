import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID, uuid5

import tiktoken
from llama_index.core.node_parser import SentenceSplitter

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


@dataclass(frozen=True, slots=True)
class _SemanticSection:
    text: str
    page_number: int | None
    title: str | None
    path: tuple[str, ...]
    metadata: dict[str, object]


class DeterministicChunker:
    """Deterministic, structure-aware chunks without crossing semantic sections."""

    default_chunk_size = 500
    default_chunk_overlap = 64

    _chinese_heading = re.compile(
        r"^第[一二三四五六七八九十百千万零〇0-9]+[章节条](?:\s*.*)?$"
    )
    _chinese_list_heading = re.compile(r"^[一二三四五六七八九十百千万零〇]+、(?:\s*.*)?$")
    _chinese_parenthetical_heading = re.compile(
        r"^[（(][一二三四五六七八九十百千万零〇0-9]+[）)](?:\s*.*)?$"
    )
    _numbered_heading = re.compile(
        r"^(?:\d+(?:\.\d+)+(?:\s+.*)?|\d+[.)、](?:\s*.*)?)$"
    )
    def __init__(
        self,
        tokenizer: Tokenizer | None = None,
        *,
        chunk_size: int = default_chunk_size,
        chunk_overlap: int = default_chunk_overlap,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size")
        self._tokenizer: Tokenizer = (
            tokenizer
            if tokenizer is not None
            else cast(Tokenizer, tiktoken.encoding_for_model("text-embedding-3-small"))
        )
        self.target_tokens = chunk_size
        self.max_tokens = chunk_size
        self.overlap_tokens = chunk_overlap

    def chunk(self, *, document_version_id: UUID, sections: list[ParsedSection]) -> list[Chunk]:
        chunks: list[Chunk] = []
        for parsed_section in sections:
            for semantic_section in self._semantic_sections(parsed_section):
                for body in self._chunk_section(semantic_section):
                    ordinal = len(chunks)
                    text = self._render(semantic_section, body)
                    chunks.append(
                        Chunk(
                            id=uuid5(document_version_id, f"chunk:{ordinal}"),
                            ordinal=ordinal,
                            text=text,
                            page_number=semantic_section.page_number,
                            section=self._database_section(semantic_section.title),
                            token_count=len(self._tokenizer.encode(text)),
                            metadata={
                                "parser": "application-parser",
                                "parser_version": "application-parser-v1",
                                **semantic_section.metadata,
                                "chunking_version": "llama-index-sentence-v1",
                                "chunk_size": self.target_tokens,
                                "chunk_overlap": self.overlap_tokens,
                                "section_title": semantic_section.title,
                                "section_path": " > ".join(semantic_section.path)
                                if semantic_section.path
                                else None,
                                "page_range": self._page_range(semantic_section.page_number),
                            },
                        )
                    )
        return chunks

    def _semantic_sections(self, parsed_section: ParsedSection) -> list[_SemanticSection]:
        text = self._normalize(parsed_section.text)
        if not text:
            return []
        parser_path = tuple(
            part.strip()
            for part in (parsed_section.section or "").split(" > ")
            if part.strip()
        )
        heading_stack: list[tuple[int, str]] = []
        current_title = parser_path[-1] if parser_path else None
        current_path = parser_path
        current_lines: list[str] = []
        semantic_sections: list[_SemanticSection] = []

        def append_current() -> None:
            body = self._normalize("\n".join(current_lines))
            if body:
                semantic_sections.append(
                    _SemanticSection(
                        text=body,
                        page_number=parsed_section.page_number,
                        title=current_title,
                        path=current_path,
                        metadata=dict(parsed_section.metadata),
                    )
                )

        for line in text.splitlines():
            if self._is_heading(line):
                append_current()
                level = self._heading_level(line)
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, line))
                current_title = line
                current_path = parser_path + tuple(title for _, title in heading_stack)
                current_lines = []
            else:
                current_lines.append(line)
        append_current()
        return semantic_sections

    def _chunk_section(self, section: _SemanticSection) -> list[str]:
        prefix_tokens = len(self._tokenizer.encode(self._prefix(section)))
        target_body_tokens = max(1, self.target_tokens - prefix_tokens)
        splitter = SentenceSplitter(
            chunk_size=target_body_tokens,
            chunk_overlap=min(self.overlap_tokens, target_body_tokens - 1),
            tokenizer=self._tokenizer.encode,
            paragraph_separator="\n\n",
            include_metadata=False,
            include_prev_next_rel=False,
        )
        return splitter.split_text(section.text)

    def _render(self, section: _SemanticSection, body: str) -> str:
        prefix = self._prefix(section)
        if not prefix:
            return body
        prefix_tokens = self._tokenizer.encode(prefix)
        while prefix_tokens:
            text = self._join(self._tokenizer.decode(prefix_tokens), body)
            if len(self._tokenizer.encode(text)) <= self.max_tokens:
                return text
            prefix_tokens.pop()
        return body

    @staticmethod
    def _join(left: str, right: str) -> str:
        if not left:
            return right
        if not right:
            return left
        return f"{left}\n\n{right}"

    @staticmethod
    def _page_range(page_number: int | None) -> dict[str, int] | None:
        if page_number is None:
            return None
        return {"start": page_number, "end": page_number}

    @staticmethod
    def _database_section(title: str | None) -> str | None:
        return title[:1024] if title is not None else None

    @staticmethod
    def _normalize(value: str) -> str:
        lines = [
            re.sub(r"[ \t\f\v]+", " ", line).strip()
            for line in unicodedata.normalize("NFC", value).replace("\r\n", "\n").split("\n")
        ]
        collapsed: list[str] = []
        for line in lines:
            if line or (collapsed and collapsed[-1]):
                collapsed.append(line)
        return "\n".join(collapsed).strip()

    def _prefix(self, section: _SemanticSection) -> str:
        if not section.path:
            return ""
        tokens = self._tokenizer.encode(" > ".join(section.path))
        body_reserve = min(64, max(1, self.max_tokens // 4))
        return self._tokenizer.decode(tokens[: self.max_tokens - body_reserve])

    def _is_heading(self, line: str) -> bool:
        return bool(
            self._chinese_heading.match(line)
            or self._chinese_list_heading.match(line)
            or self._chinese_parenthetical_heading.match(line)
            or self._numbered_heading.match(line)
        )

    def _heading_level(self, line: str) -> int:
        if "章" in line:
            return 1
        if "节" in line:
            return 2
        if "条" in line or self._chinese_list_heading.match(line):
            return 3
        if self._chinese_parenthetical_heading.match(line):
            return 4
        numbered = re.match(r"^(\d+(?:\.\d+)*)", line)
        return numbered.group(1).count(".") + 1 if numbered else 1
