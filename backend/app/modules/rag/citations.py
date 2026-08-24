from app.modules.rag.types import AnswerAudience, CustomerCitation, RetrievedChunk, SourceCitation


def citation_from_chunk(chunk: RetrievedChunk) -> SourceCitation:
    return SourceCitation(
        chunk_id=chunk.chunk_id,
        document_version_id=chunk.document_version_id,
        title=chunk.title or "Knowledge base document",
        section=chunk.section,
        page_number=chunk.page_number,
        internal_drive_link=chunk.internal_drive_link,
    )


def project_citations(
    chunks: list[RetrievedChunk], audience: AnswerAudience
) -> list[CustomerCitation | SourceCitation]:
    return [citation_from_chunk(chunk).for_audience(audience) for chunk in chunks]
