import re
import unicodedata
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from docx import Document as WordDocument
from llama_index.readers.file import PDFReader  # type: ignore[import-untyped]


class DocumentParseError(Exception):
    def __init__(self, code: str = "DOCUMENT_PARSE_FAILED") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ParsedSection:
    text: str
    page_number: int | None
    section: str | None
    metadata: dict[str, object] = field(default_factory=dict)


class DocumentParser(Protocol):
    def parse(self, content: bytes) -> list[ParsedSection]: ...


def _normalize(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", unicodedata.normalize("NFC", value)).strip()


class PdfParser:
    def parse(self, content: bytes) -> list[ParsedSection]:
        try:
            with TemporaryDirectory(prefix="knowledge-pdf-") as directory:
                path = Path(directory) / "document.pdf"
                path.write_bytes(content)
                pages = PDFReader(return_full_document=False).load_data(path)
        except Exception as exc:
            error_names = {type(error).__name__ for error in _exception_chain(exc)}
            code = (
                "PDF_ENCRYPTED"
                if error_names & {"FileNotDecryptedError", "WrongPasswordError"}
                else "PDF_CORRUPT"
            )
            raise DocumentParseError(code) from exc
        if not pages:
            raise DocumentParseError("PDF_EMPTY")
        sections = [
            ParsedSection(
                text=normalized,
                page_number=index,
                section=None,
                metadata={
                    "parser": "llama-index-pdf-reader",
                    "parser_version": "pdf-reader-v1",
                    "page_number_kind": "physical",
                },
            )
            for index, page in enumerate(pages, start=1)
            if (normalized := _normalize(page.text or ""))
        ]
        if not sections:
            raise DocumentParseError("PDF_OCR_REQUIRED")
        return sections


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


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
                    ParsedSection(
                        text=normalized,
                        page_number=None,
                        section=current_heading,
                        metadata={
                            "parser": "python-docx",
                            "parser_version": "word-parser-v1",
                        },
                    )
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
