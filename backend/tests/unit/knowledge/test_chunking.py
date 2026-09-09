from uuid import UUID

import pytest

from app.modules.knowledge.chunking import DeterministicChunker
from app.modules.knowledge.parsers import ParsedSection


class WhitespaceTokenizer:
    def encode(self, value: str) -> list[str]:
        return value.split()

    def decode(self, tokens: list[str]) -> str:
        return " ".join(tokens)


def test_chunker_uses_target_and_overlap_without_crossing_sections() -> None:
    text = " ".join(f"token-{index}" for index in range(780))
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[ParsedSection(text=text, page_number=3, section="Eligibility")],
    )

    assert [chunk.token_count for chunk in chunks] == [500, 346]
    assert chunks[0].section == "Eligibility"
    assert chunks[0].page_number == 3
    assert all(chunk.text.startswith("Eligibility") for chunk in chunks)
    assert all(chunk.metadata["section_path"] == "Eligibility" for chunk in chunks)
    assert all(chunk.metadata["page_range"] == {"start": 3, "end": 3} for chunk in chunks)
    assert chunks[0].text.split()[-64:] == chunks[1].text.split()[1:65]
    assert all(chunk.token_count <= 800 for chunk in chunks)


def test_chunker_is_deterministic_for_ids_and_boundaries() -> None:
    sections = [
        ParsedSection(text="alpha beta gamma", page_number=1, section="One"),
        ParsedSection(text="delta epsilon", page_number=2, section="Two"),
    ]
    version_id = UUID("9b652e7c-c891-4e53-9152-0d8079276c8a")

    first = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=version_id, sections=sections
    )
    second = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=version_id, sections=sections
    )

    assert [(chunk.id, chunk.text, chunk.ordinal) for chunk in first] == [
        (chunk.id, chunk.text, chunk.ordinal) for chunk in second
    ]


def test_chunker_keeps_chinese_policy_articles_as_separate_semantic_sections() -> None:
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[
            ParsedSection(
                text=(
                    "第一条 总则\n本制度适用于全体员工。\n\n"
                    "第二条 适用范围\n适用于公司及其分支机构。\n\n"
                    "第三条 职责\n人力资源部门负责解释。\n\n"
                    "第四条 生效\n本制度自发布之日起生效。"
                ),
                page_number=1,
                section=None,
            )
        ],
    )

    assert [chunk.metadata["section_title"] for chunk in chunks] == [
        "第一条 总则",
        "第二条 适用范围",
        "第三条 职责",
        "第四条 生效",
    ]
    assert [chunk.metadata["section_path"] for chunk in chunks] == [
        "第一条 总则",
        "第二条 适用范围",
        "第三条 职责",
        "第四条 生效",
    ]
    assert all(chunk.metadata["chunking_version"] == "structural-v1" for chunk in chunks)
    assert all(chunk.token_count <= 800 for chunk in chunks)
    assert all("\n\n第二条" not in chunk.text for chunk in chunks)
    assert all("\n\n第三条" not in chunk.text for chunk in chunks)
    assert all("\n\n第四条" not in chunk.text for chunk in chunks)


def test_chunker_uses_paragraphs_then_sentences_for_unstructured_text() -> None:
    first_paragraph = " ".join(f"first-{index}" for index in range(420))
    second_paragraph = " ".join(f"second-{index}" for index in range(420))
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[
            ParsedSection(
                text=f"{first_paragraph}\n\n{second_paragraph}",
                page_number=1,
                section=None,
            )
        ],
    )

    assert len(chunks) == 2
    assert chunks[0].text.endswith("first-419")
    assert "second-0" in chunks[1].text
    assert all(chunk.token_count <= 800 for chunk in chunks)


def test_chunker_splits_oversized_section_by_paragraph_without_cross_section_overlap() -> None:
    paragraphs = [
        " ".join(f"paragraph-{part}-{index}" for index in range(350))
        for part in range(3)
    ]
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[
            ParsedSection(
                text="1. Scope\n" + "\n\n".join(paragraphs),
                page_number=2,
                section=None,
            )
        ],
    )

    assert len(chunks) == 3
    assert all(chunk.token_count <= 800 for chunk in chunks)
    assert all(chunk.metadata["section_title"] == "1. Scope" for chunk in chunks)
    assert all(chunk.text.startswith("1. Scope") for chunk in chunks)


def test_chunker_uses_token_fallback_for_a_long_single_paragraph() -> None:
    text = " ".join(f"token-{index}" for index in range(900))
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[ParsedSection(text=text, page_number=None, section=None)],
    )

    assert len(chunks) == 2
    assert all(chunk.token_count <= 800 for chunk in chunks)
    assert chunks[0].text.split()[-64:] == chunks[1].text.split()[:64]


def test_chunker_never_overlaps_across_detected_sections() -> None:
    first_body = " ".join(f"first-{index}" for index in range(780))
    second_body = " ".join(f"second-{index}" for index in range(780))
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[
            ParsedSection(
                text=f"第一条 第一部分\n{first_body}\n\n第二条 第二部分\n{second_body}",
                page_number=1,
                section=None,
            )
        ],
    )

    first_section_chunks = [
        chunk for chunk in chunks if chunk.metadata["section_title"] == "第一条 第一部分"
    ]
    second_section_chunks = [
        chunk for chunk in chunks if chunk.metadata["section_title"] == "第二条 第二部分"
    ]
    assert first_section_chunks
    assert second_section_chunks
    assert all("second-" not in chunk.text for chunk in first_section_chunks)
    assert all("first-" not in chunk.text for chunk in second_section_chunks)


def test_chunker_keeps_a_short_document_in_one_chunk() -> None:
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[ParsedSection(text="A short document.", page_number=1, section=None)],
    )

    assert len(chunks) == 1
    assert chunks[0].text == "A short document."
    assert chunks[0].metadata["chunking_version"] == "structural-v1"


def test_chunker_keeps_an_oversized_heading_within_the_token_ceiling() -> None:
    heading = "1. " + " ".join(f"heading-{index}" for index in range(850))
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[ParsedSection(text=f"{heading}\nEvidence.", page_number=1, section=None)],
    )

    assert all(chunk.token_count <= 800 for chunk in chunks)
    assert chunks[0].metadata["section_title"] == heading


def test_chunker_enforces_the_ceiling_with_the_production_tokenizer() -> None:
    heading = "1. " + " ".join(f"heading-{index}" for index in range(850))
    chunks = DeterministicChunker().chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[ParsedSection(text=f"{heading}\nEvidence.", page_number=1, section=None)],
    )

    assert all(chunk.token_count <= 800 for chunk in chunks)


@pytest.mark.parametrize("heading", ("第一条", "第一章", "一、", "（一）", "1.", "1.1"))
def test_chunker_treats_standalone_heading_markers_as_hard_boundaries(heading: str) -> None:
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[
            ParsedSection(
                text=f"{heading}\nFirst body.\n\n{heading}\nSecond body.",
                page_number=1,
                section=None,
            )
        ],
    )

    assert len(chunks) == 2
    assert all(chunk.metadata["section_title"] == heading for chunk in chunks)
    assert chunks[0].text.endswith("First body.")
    assert chunks[1].text.endswith("Second body.")


@pytest.mark.parametrize(
    "heading",
    ("第一章 总则", "第一节 范围", "一、目的", "（一）定义", "1.1 Scope"),
)
def test_chunker_recognizes_common_heading_forms(heading: str) -> None:
    chunks = DeterministicChunker(tokenizer=WhitespaceTokenizer()).chunk(
        document_version_id=UUID("9b652e7c-c891-4e53-9152-0d8079276c8a"),
        sections=[ParsedSection(text=f"{heading}\nEvidence.", page_number=1, section=None)],
    )

    assert [chunk.metadata["section_title"] for chunk in chunks] == [heading]
    assert chunks[0].text.startswith(heading)
