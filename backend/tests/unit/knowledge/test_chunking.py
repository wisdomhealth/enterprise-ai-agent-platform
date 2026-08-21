from uuid import UUID

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

    assert [chunk.token_count for chunk in chunks] == [700, 180]
    assert chunks[0].section == "Eligibility"
    assert chunks[0].page_number == 3
    assert chunks[0].text.split()[-100:] == chunks[1].text.split()[:100]


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
