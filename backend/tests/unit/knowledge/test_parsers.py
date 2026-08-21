from pathlib import Path

import pytest

from app.modules.knowledge.parsers import DocumentParseError, DocumentParser, PdfParser, WordParser

FIXTURE_DIRECTORY = Path("tests/fixtures/documents")


def test_pdf_parser_preserves_page_citation() -> None:
    sections = PdfParser().parse((FIXTURE_DIRECTORY / "sample.pdf").read_bytes())

    assert sections[0].page_number == 1
    assert "Customer support policy" in sections[0].text


def test_word_parser_preserves_heading_as_section() -> None:
    sections = WordParser().parse((FIXTURE_DIRECTORY / "sample.docx").read_bytes())

    assert sections[0].section == "Escalation"
    assert "Contact the support team" in sections[0].text


@pytest.mark.parametrize(
    ("parser", "content"),
    [(PdfParser(), b"not-a-pdf"), (WordParser(), b"not-a-docx")],
)
def test_parser_raises_safe_error_for_invalid_document(
    parser: DocumentParser, content: bytes
) -> None:
    with pytest.raises(DocumentParseError) as error:
        parser.parse(content)

    assert error.value.code == "DOCUMENT_PARSE_FAILED"
