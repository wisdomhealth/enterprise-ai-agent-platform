from hashlib import sha256

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.chunking import DeterministicChunker
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
)
from app.modules.knowledge.parsers import DocumentParseError, DocumentParser, PdfParser, WordParser


class DocumentIngestionService:
    def __init__(
        self, db_session: AsyncSession, *, chunker: DeterministicChunker | None = None
    ) -> None:
        self._db_session = db_session
        self._chunker = chunker or DeterministicChunker()

    async def parse_bytes(
        self,
        document: Document,
        content: bytes,
        mime_type: str,
        parser: DocumentParser | None = None,
    ) -> DocumentVersion:
        return await self.ingest_bytes(document, content, mime_type, parser)

    async def ingest_bytes(
        self,
        document: Document,
        content: bytes,
        mime_type: str,
        parser: DocumentParser | None = None,
    ) -> DocumentVersion:
        content_hash = sha256(content).hexdigest()
        version = DocumentVersion(
            document_id=document.id,
            state=DocumentVersionState.PROCESSING,
            content_sha256=content_hash,
        )
        self._db_session.add(version)
        await self._db_session.flush()
        selected_parser = parser or self._parser_for(mime_type)
        try:
            sections = selected_parser.parse(content)
            chunks = self._chunker.chunk(document_version_id=version.id, sections=sections)
            self._db_session.add_all(
                DocumentChunk(
                    id=chunk.id,
                    document_version_id=version.id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    page_number=chunk.page_number,
                    section=chunk.section,
                    token_count=chunk.token_count,
                    metadata_=chunk.metadata,
                )
                for chunk in chunks
            )
            await self._db_session.flush()
        except DocumentParseError as exc:
            version.state = DocumentVersionState.FAILED
            version.error_code = exc.code
            await self._db_session.flush()
            raise
        except Exception as exc:
            version.state = DocumentVersionState.FAILED
            version.error_code = "DOCUMENT_PARSE_FAILED"
            await self._db_session.flush()
            raise DocumentParseError() from exc
        return version

    @staticmethod
    def _parser_for(mime_type: str) -> DocumentParser:
        if mime_type == "application/pdf":
            return PdfParser()
        if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return WordParser()
        raise DocumentParseError("UNSUPPORTED_DOCUMENT_TYPE")
