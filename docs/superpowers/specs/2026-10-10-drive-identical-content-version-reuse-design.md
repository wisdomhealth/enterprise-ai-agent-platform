# Drive Identical-Content Version Reuse Design

## Goal

Make Drive rename and identical-content deliveries idempotent while preserving document identity, preventing stale publication, and atomically replacing retrievable versions when content changes.

## Root cause

Drive synchronization already locates documents by `(organization_id, source_id, external_id)` and updates the title. Parsing currently computes the content hash only after parsing and chunking, then blindly inserts a `DocumentVersion`. A rename or identical-content delivery therefore reaches `uq_document_versions_content`; the generic failure handler classifies the integrity error as retryable and repeats it.

## Design

After the authorized download finishes, calculate SHA-256 before parsing. End the read transaction, then use the shared write order `DriveSource -> JobIntent rows ordered by ID -> Document -> DocumentVersion rows ordered by ID -> DocumentChunk rows ordered by version and chunk ID`.

Under those locks, validate source ownership/status/scope, the current lease generation, task ordering, document ownership, and version ownership. A matching `RETRIEVABLE` version is associated with the job and reused without parsing or embedding. A matching `PROCESSING` version is resumed only when already referenced by the current job. `PROCESSING` owned by another job, `FAILED`, `REVOKED`, and `DELETED` produce explicit non-retryable errors.

If no match exists, release the transaction and parse/chunk in memory. Reacquire the shared locks and repeat the hash lookup before inserting the new `PROCESSING` version and chunks. This closes the concurrency window without holding write locks during Drive, parser, or embedding work.

At final publication, reacquire the same locks, revalidate the lease/source/document/task generation, and lock the target plus actual `current_version_id`. In one transaction: persist embeddings, mark the target `RETRIEVABLE`, switch `current_version_id`, revoke the replaced version, delete only its chunks, write `knowledge.document.version.replaced`, and complete the parse job. If target equals current, only job association/completion is required. Any exception rolls back the entire transition.

## Retrieval and references

Vector/full-text retrieval already shares `_authorized_chunks_query`, which requires organization ownership, authorized knowledge access, an ACTIVE Drive source, `Document.current_version_id == DocumentVersion.id`, and `DocumentVersion.state == RETRIEVABLE`. Email evidence projection applies the same current/retrievable/source constraints. Regression tests will freeze these guarantees.

There are no relational foreign keys from chat, email, or citation tables to `document_chunks`; persisted citations are JSON or UUID arrays. Chunk deletion therefore does not cascade-delete business rows. Historical citation projection may no longer resolve physically deleted evidence chunks; the citation-bearing business records remain intact.

## State handling

- `RETRIEVABLE`: reuse only within the same document after all publication checks.
- `PROCESSING`: resume only through the current job's existing `document_version_id`; another job receives `DOCUMENT_CONTENT_PROCESSING` as non-retryable.
- `FAILED`: `DOCUMENT_CONTENT_FAILED`, non-retryable.
- `REVOKED`: `DOCUMENT_CONTENT_REVOKED`, non-retryable; automatic restoration is outside scope.
- `DELETED`: `DOCUMENT_CONTENT_DELETED`, non-retryable.

## Testing

Use the isolated PostgreSQL test database and fake Drive/parser/embedding implementations. Cover rename identity/title, pre-parse reuse, duplicate delivery, changed content replacement/cleanup/audit, state errors, cross-document isolation, concurrency, rollback, stale workers, retrieval filtering, and preservation of unrelated organization/business rows.
