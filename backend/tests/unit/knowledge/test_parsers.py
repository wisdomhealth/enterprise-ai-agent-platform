from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.modules.knowledge.parsers import DocumentParseError, DocumentParser, PdfParser, WordParser

FIXTURE_DIRECTORY = Path("tests/fixtures/documents")


def test_pdf_parser_preserves_page_citation() -> None:
    sections = PdfParser().parse((FIXTURE_DIRECTORY / "sample.pdf").read_bytes())

    assert sections[0].page_number == 1
    assert "Customer support policy" in sections[0].text
    assert sections[0].metadata["parser"] == "llama-index-pdf-reader"
    assert sections[0].metadata["page_number_kind"] == "physical"


def _pdf_bytes(*pages: str) -> bytes:
    output = BytesIO()
    document = canvas.Canvas(output)
    for index, text in enumerate(pages):
        document.drawString(72, 720, text)
        if index < len(pages) - 1:
            document.showPage()
    document.save()
    return output.getvalue()


def test_pdf_parser_uses_one_based_physical_page_numbers() -> None:
    sections = PdfParser().parse(_pdf_bytes("First physical page", "Second physical page"))

    assert [(section.page_number, section.text) for section in sections] == [
        (1, "First physical page"),
        (2, "Second physical page"),
    ]


def test_pdf_parser_extracts_chinese_text() -> None:
    output = BytesIO()
    document = canvas.Canvas(output)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    document.setFont("STSong-Light", 12)
    document.drawString(72, 720, "第一章 客户支持政策")
    document.save()

    sections = PdfParser().parse(output.getvalue())

    assert sections[0].text == "第一章 客户支持政策"


def test_pdf_parser_rejects_encrypted_pdf_with_safe_code() -> None:
    reader = PdfReader(BytesIO(_pdf_bytes("Secret")))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt("password")
    output = BytesIO()
    writer.write(output)

    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(output.getvalue())

    assert error.value.code == "PDF_ENCRYPTED"


def test_pdf_parser_reports_empty_pdf() -> None:
    writer = PdfWriter()
    output = BytesIO()
    writer.write(output)

    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(output.getvalue())

    assert error.value.code == "PDF_EMPTY"


def test_pdf_parser_reports_ocr_for_pages_without_usable_text() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)

    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(output.getvalue())

    assert error.value.code == "PDF_OCR_REQUIRED"


def test_word_parser_preserves_heading_as_section() -> None:
    sections = WordParser().parse((FIXTURE_DIRECTORY / "sample.docx").read_bytes())

    assert sections[0].section == "Escalation"
    assert "Contact the support team" in sections[0].text
    assert sections[0].metadata == {
        "parser": "python-docx",
        "parser_version": "word-parser-v1",
    }


@pytest.mark.parametrize(
    ("parser", "content"),
    [(PdfParser(), b"not-a-pdf"), (WordParser(), b"not-a-docx")],
)
def test_parser_raises_safe_error_for_invalid_document(
    parser: DocumentParser, content: bytes
) -> None:
    with pytest.raises(DocumentParseError) as error:
        parser.parse(content)

    expected = "PDF_CORRUPT" if isinstance(parser, PdfParser) else "DOCUMENT_PARSE_FAILED"
    assert error.value.code == expected
