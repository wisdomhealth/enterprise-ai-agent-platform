import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from docx import Document as WordDocument
from pypdf import PdfReader


class DocumentParseError(Exception):
    def __init__(self, code: str = "DOCUMENT_PARSE_FAILED") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ParsedSection:
    text: str
    page_number: int | None
    section: str | None


class DocumentParser(Protocol):
    def parse(self, content: bytes) -> list[ParsedSection]: ...


def _normalize(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", unicodedata.normalize("NFC", value)).strip()


class PdfParser:
    def parse(self, content: bytes) -> list[ParsedSection]:
        try:
            reader = PdfReader(BytesIO(content))
            sections = [
                ParsedSection(text=normalized, page_number=index, section=None)
                for index, page in enumerate(reader.pages, start=1)
                if (normalized := _normalize(page.extract_text() or ""))
            ]
        except Exception as exc:
            raise DocumentParseError() from exc
        if not sections:
            raise DocumentParseError()
        return sections


class WordParser:
    def parse(self, content: bytes) -> list[ParsedSection]:
        try:
            document = WordDocument(BytesIO(content))
        except Exception as exc:
            raise DocumentParseError() from exc

        sections: list[ParsedSection] = []
        current_heading: str | None = None
        current_lines: list[str] = []

        def append_current() -> None:
            normalized = _normalize("\n".join(current_lines))
            if normalized:
                sections.append(
                    ParsedSection(text=normalized, page_number=None, section=current_heading)
                )

        for paragraph in document.paragraphs:
            text = _normalize(paragraph.text)
            if not text:
                continue
            style_name = paragraph.style.name if paragraph.style is not None else ""
            if style_name.startswith("Heading"):
                append_current()
                current_heading = text
                current_lines = []
            else:
                current_lines.append(text)
        append_current()
        if not sections:
            raise DocumentParseError()
        return sections
