from dataclasses import replace
from uuid import uuid4

import pytest

from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.rag.groundedness import CitationValidator, GroundednessError
from app.modules.rag.llm import GeneratedAnswer
from app.modules.rag.types import ClaimSupport, RetrievedChunk


def _principal() -> Principal:
    return Principal(uuid4(), uuid4(), "member@example.test", UserRole.MEMBER, uuid4(), "csrf")


def _chunk(
    principal: Principal, *, text: str = "Refunds take five business days."
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid4(),
        stable_id="refund-policy",
        document_version_id=uuid4(),
        document_id=uuid4(),
        organization_id=principal.organization_id,
        knowledge_base_id=uuid4(),
        ordinal=0,
        text=text,
        page_number=2,
        section="Eligibility",
        resource_authorized=True,
        title="Refund policy",
        internal_drive_link="https://drive.google.com/private",
    )


def test_validator_accepts_material_claim_supported_by_retrieved_authorized_chunk() -> None:
    principal = _principal()
    chunk = _chunk(principal, text="Refunds take five business days.")
    generation = GeneratedAnswer(
        text="Refunds take five business days.",
        claims=[
            ClaimSupport(text="Refunds take five business days.", citation_ids=[chunk.chunk_id])
        ],
        model="claude-test",
        input_tokens=10,
        output_tokens=8,
    )

    assert CitationValidator().validate(
        generation, [chunk], principal, chunk.knowledge_base_id
    ) == [chunk]


@pytest.mark.parametrize("reason", ["unknown", "unauthorized", "wrong_organization", "wrong_kb"])
def test_validator_rejects_claim_citation_outside_authorized_retrieval_set(reason: str) -> None:
    principal = _principal()
    chunk = _chunk(principal)
    citation_id = chunk.chunk_id
    knowledge_base_id = chunk.knowledge_base_id
    if reason == "unknown":
        citation_id = uuid4()
    elif reason == "unauthorized":
        chunk = replace(chunk, resource_authorized=False)
    elif reason == "wrong_organization":
        chunk = replace(chunk, organization_id=uuid4())
    else:
        chunk = replace(chunk, knowledge_base_id=uuid4())
    generation = GeneratedAnswer(
        text="Refunds take five business days.",
        claims=[ClaimSupport(text="Refunds take five business days.", citation_ids=[citation_id])],
        model="claude-test",
        input_tokens=10,
        output_tokens=8,
    )

    with pytest.raises(GroundednessError):
        CitationValidator().validate(generation, [chunk], principal, knowledge_base_id)


def test_validator_rejects_material_claim_not_supported_by_cited_text() -> None:
    principal = _principal()
    chunk = _chunk(principal)
    generation = GeneratedAnswer(
        text="Refunds take one hour.",
        claims=[ClaimSupport(text="Refunds take one hour.", citation_ids=[chunk.chunk_id])],
        model="claude-test",
        input_tokens=10,
        output_tokens=8,
    )

    with pytest.raises(GroundednessError, match="support"):
        CitationValidator().validate(generation, [chunk], principal, chunk.knowledge_base_id)


def test_validator_requires_citations_even_when_model_marks_claim_non_material() -> None:
    principal = _principal()
    chunk = _chunk(principal)
    generation = GeneratedAnswer(
        text="Refunds take one hour.",
        claims=[
            ClaimSupport(
                text="Refunds take one hour.",
                citation_ids=[],
                material=False,
            )
        ],
        model="claude-test",
        input_tokens=10,
        output_tokens=8,
    )

    with pytest.raises(GroundednessError, match="supporting citations"):
        CitationValidator().validate(generation, [chunk], principal, chunk.knowledge_base_id)


def test_validator_rejects_answer_sentence_not_covered_by_an_atomic_claim() -> None:
    principal = _principal()
    chunk = _chunk(principal, text="Refunds take five business days.")
    generation = GeneratedAnswer(
        text="Refunds take five business days. Refunds arrive in one hour.",
        claims=[
            ClaimSupport(text="Refunds take five business days.", citation_ids=[chunk.chunk_id])
        ],
        model="claude-test",
        input_tokens=10,
        output_tokens=8,
    )

    with pytest.raises(GroundednessError, match="atomic claim"):
        CitationValidator().validate(generation, [chunk], principal, chunk.knowledge_base_id)
