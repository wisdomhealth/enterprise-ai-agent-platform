from collections.abc import Awaitable, Callable
from uuid import UUID

from app.modules.jobs.models import JobIntent
from app.modules.knowledge.models import Document, DocumentVersion
from app.modules.knowledge.sync import SyncResult


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


class DriveSyncTask:
    """Worker boundary for scheduled and manually requested sync intents."""

    def __init__(self, sync_source: Callable[[UUID, str | None], Awaitable[SyncResult]]) -> None:
        self._sync_source = sync_source

    async def run(self, source_id: UUID, page_token: str | None) -> SyncResult:
        return await self._sync_source(source_id, page_token)
