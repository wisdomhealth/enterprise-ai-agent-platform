from uuid import uuid4

from app.modules.rag.types import AnswerAudience, SourceCitation


def test_customer_projection_removes_internal_source_fields() -> None:
    citation = SourceCitation(
        chunk_id=uuid4(),
        document_version_id=uuid4(),
        title="Refund policy",
        section="Eligibility",
        page_number=2,
        internal_drive_link="https://drive.google.com/private",
    )

    assert citation.for_audience(AnswerAudience.CUSTOMER).model_dump() == {
        "title": "Refund policy",
        "section": "Eligibility",
        "page_number": 2,
    }


def test_staff_projection_retains_authorized_internal_source_fields() -> None:
    chunk_id = uuid4()
    version_id = uuid4()
    citation = SourceCitation(
        chunk_id=chunk_id,
        document_version_id=version_id,
        title="Refund policy",
        section="Eligibility",
        page_number=2,
        internal_drive_link="https://drive.google.com/private",
    )

    assert citation.for_audience(AnswerAudience.STAFF).model_dump() == {
        "chunk_id": chunk_id,
        "document_version_id": version_id,
        "title": "Refund policy",
        "section": "Eligibility",
        "page_number": 2,
        "internal_drive_link": "https://drive.google.com/private",
    }
