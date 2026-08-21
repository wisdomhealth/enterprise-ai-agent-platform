from collections.abc import Awaitable, Callable
from uuid import UUID

from app.modules.jobs.models import JobIntent
from app.modules.knowledge.models import Document, DocumentVersion


class DocumentParseTask:
    """Task boundary used by workers after a durable document-parse job has been claimed."""

    def __init__(
        self,
        parse_job: Callable[[JobIntent, Document], Awaitable[DocumentVersion]],
    ) -> None:
        self._parse_job = parse_job

    async def run(self, job: JobIntent, document: Document) -> DocumentVersion:
        return await self._parse_job(job, document)


def document_parse_job_key(document_id: UUID, content_sha256: str) -> str:
    return f"document-parse:{document_id}:{content_sha256}"
