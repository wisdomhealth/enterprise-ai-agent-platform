import re
import unicodedata
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


@dataclass(frozen=True, slots=True)
class _SemanticSection:
    text: str
    page_number: int | None
    title: str | None
    path: tuple[str, ...]


class DeterministicChunker:
    """Deterministic, structure-aware chunks without crossing semantic sections."""

    target_tokens = 500
    max_tokens = 800
    overlap_tokens = 64

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
    _sentence_boundary = re.compile(r"(?<=[。！？!?])\s*")

    def __init__(self, tokenizer: Tokenizer | None = None) -> None:
        self._tokenizer: Tokenizer = (
            tokenizer
            if tokenizer is not None
            else cast(Tokenizer, tiktoken.encoding_for_model("text-embedding-3-small"))
        )

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
                                "chunking_version": "structural-v1",
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
        max_body_tokens = max(1, self.max_tokens - prefix_tokens)
        units = self._section_units(section.text, target_body_tokens)
        bodies: list[str] = []
        current = ""

        for unit in units:
            candidate = self._join(current, unit)
            if current and len(self._tokenizer.encode(candidate)) > target_body_tokens:
                bodies.append(current)
                current = unit
            else:
                current = candidate
            if len(self._tokenizer.encode(current)) > max_body_tokens:
                bodies.extend(self._token_windows(current, target_body_tokens))
                current = ""
        if current:
            bodies.append(current)

        return self._with_overlap(bodies, max_body_tokens)

    def _section_units(self, text: str, target_body_tokens: int) -> list[str]:
        units: list[str] = []
        for paragraph in (part.strip() for part in re.split(r"\n\s*\n", text)):
            if not paragraph:
                continue
            if len(self._tokenizer.encode(paragraph)) <= target_body_tokens:
                units.append(paragraph)
                continue
            for sentence in self._sentences(paragraph):
                if len(self._tokenizer.encode(sentence)) <= target_body_tokens:
                    units.append(sentence)
                else:
                    units.extend(self._token_windows(sentence, target_body_tokens))
        return units

    def _with_overlap(self, bodies: list[str], max_body_tokens: int) -> list[str]:
        if not bodies:
            return []
        overlapped = [bodies[0]]
        for previous, current in zip(bodies, bodies[1:]):
            overlap = self._tail(previous, self.overlap_tokens)
            candidate = self._join(overlap, current)
            if len(self._tokenizer.encode(candidate)) <= max_body_tokens:
                overlapped.append(candidate)
            else:
                allowed = max(0, max_body_tokens - len(self._tokenizer.encode(current)))
                overlapped.append(self._join(self._tail(previous, allowed), current))
        return overlapped

    def _token_windows(self, text: str, size: int) -> list[str]:
        tokens = self._tokenizer.encode(text)
        return [
            self._tokenizer.decode(tokens[start : start + size])
            for start in range(0, len(tokens), size)
        ]

    def _tail(self, text: str, count: int) -> str:
        if count <= 0:
            return ""
        return self._tokenizer.decode(self._tokenizer.encode(text)[-count:])

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
        return self._tokenizer.decode(tokens[: self.max_tokens - 1])

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

    def _sentences(self, paragraph: str) -> list[str]:
        return [sentence for sentence in self._sentence_boundary.split(paragraph) if sentence]
