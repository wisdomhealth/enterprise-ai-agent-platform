# LlamaIndex Ingestion And Drive Cleanup Implementation Plan

> **For agentic workers:** Execute inline in this session. Do not create a worktree, switch branches, commit, push, or merge. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use local LlamaIndex adapters for PDF parsing, structure-aware chunking, and OpenAI embeddings while completing the durable Drive-deletion cleanup path that physically deletes only chunks belonging to the explicitly revoked versions.

**Architecture:** Keep Google Drive authorization, SQLAlchemy models, pgvector storage, job leases, and atomic version publication as the system of record. LlamaIndex is an adapter inside the existing ingestion service: `PDFReader` emits physical-page sections, `SentenceSplitter` splits one semantic page/section at a time, and `OpenAIEmbedding` implements the existing asynchronous provider protocol. Drive deletion fences outstanding parse jobs, revokes exact versions, and emits an Outbox event containing those version IDs; a retrying Celery consumer validates the ownership/state tuple before deleting matching chunk rows and recording idempotent processing.

**Tech Stack:** Python 3.12, LlamaIndex Core 0.14.25, LlamaIndex file readers 0.5.6, LlamaIndex OpenAI embeddings 0.5.2, SQLAlchemy async/PostgreSQL/pgvector, Celery/Redis, pytest, Ruff, mypy.

---

### Task 1: Pin And Install The Minimal LlamaIndex Integrations

**Files:**
- Modify: `backend/pyproject.toml`

- [ ] Add exact compatible pins for `llama-index-readers-file==0.5.6` and `llama-index-embeddings-openai==0.5.2`; retain `llama-index-core==0.14.25` and the existing OpenAI model/dimension.
- [ ] Install the project in the existing backend virtual environment.
- [ ] Verify imports for `PDFReader`, `SentenceSplitter`, and `OpenAIEmbedding` without making provider API calls.

### Task 2: Parse PDFs Locally Through LlamaIndex

**Files:**
- Modify: `backend/tests/unit/knowledge/test_parsers.py`
- Modify: `backend/app/modules/knowledge/parsers.py`
- Add or generate test fixtures only under: `backend/tests/fixtures/documents/`

- [ ] Add failing tests for multiple physical pages, Chinese text, encrypted PDF, corrupt PDF, empty PDF, and image-only/no-usable-text PDF.
- [ ] Run the parser tests and confirm the new cases fail for missing behavior.
- [ ] Adapt `llama_index.readers.file.PDFReader` to the existing `ParsedSection` contract using a temporary local file, mapping reader page metadata to one-based physical PDF pages and never inventing headings.
- [ ] Normalize extracted text as before and map failures to safe explicit error codes, including `PDF_ENCRYPTED`, `PDF_CORRUPT`, and `PDF_OCR_REQUIRED`.
- [ ] Run parser tests and confirm PDF behavior passes while Word parsing remains unchanged.

### Task 3: Split Existing Semantic Sections With LlamaIndex

**Files:**
- Modify: `backend/tests/unit/knowledge/test_chunking.py`
- Modify: `backend/app/modules/knowledge/chunking.py`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`

- [ ] Add failing tests for configurable chunk size/overlap, Chinese sentence boundaries, heading context, no cross-page/section chunks, deterministic IDs, token counts, and parser/chunker metadata.
- [ ] Run the chunking tests and confirm failure against the token-window implementation.
- [ ] Preserve heading detection/path construction, then call one LlamaIndex `SentenceSplitter` per semantic section so boundaries cannot cross pages or sections.
- [ ] Keep deterministic UUIDv5 IDs based on document version, ordinal, and the fixed chunker version; embed only rendered chunk text (section path plus body), excluding internal IDs and Drive links.
- [ ] Read chunk size and overlap from validated settings with defaults matching current behavior (`500` and `64`), and persist only per-chunk parser/chunker version and effective options in existing JSON metadata.
- [ ] Run the chunking tests and existing ingestion unit tests.

### Task 4: Embed Documents And Queries Through LlamaIndex

**Files:**
- Modify: `backend/tests/unit/rag/test_embedding_adapter.py`
- Modify: `backend/app/modules/rag/embeddings.py`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`

- [ ] Add failing tests for async batching, ordering, configured model/dimension/batch size/timeout/retries, wrong count, wrong dimensions, non-finite values, and identical document/query provider configuration.
- [ ] Run the embedding adapter tests and confirm the new adapter expectations fail.
- [ ] Wrap `llama_index.embeddings.openai.OpenAIEmbedding` behind the existing `EmbeddingProvider.embed()` protocol and use `aget_text_embedding_batch`; retain one provider for both publication and query embedding.
- [ ] Configure dimensions as 1536, bound SDK retries independently from the job retry layer, and validate count/order/dimension/finite values before returning.
- [ ] Keep `EmbeddingPublicationService` and SQLAlchemy/pgvector persistence unchanged except for consuming the validated adapter output.
- [ ] Run embedding, publication, and retrieval tests with fake responses only.

### Task 5: Make Drive Trash And Permanent Deletion Equivalent

**Files:**
- Modify: `backend/tests/unit/knowledge/test_drive_gateway.py` or the closest existing Drive gateway test
- Modify: `backend/app/modules/knowledge/drive_gateway.py`

- [ ] Add a failing test showing a change whose file metadata has `trashed=true` becomes `DriveFile.removed=True`, while ordinary metadata and transient API failures are not converted to deletion.
- [ ] Request the `trashed` field from Drive changes and combine it with the change-level `removed` flag in memory only.
- [ ] Run the Drive gateway and source-authorization tests.

### Task 6: Fence Parse Jobs And Emit Version-Scoped Cleanup Events

**Files:**
- Modify: `backend/tests/integration/knowledge/test_authorization_loss.py`
- Modify: `backend/tests/integration/knowledge/test_incremental_sync.py`
- Modify: `backend/app/modules/knowledge/sync.py`
- Modify: `backend/app/modules/knowledge/ingestion.py`

- [ ] Add failing integration tests asserting deletion invalidates pending/running parse jobs, revokes all and only the affected document's active versions, clears `current_version_id`, and writes organization/source/document/exact version IDs into the cleanup event.
- [ ] Add a failing race regression proving an old claimed parse cannot checkpoint or publish after deletion.
- [ ] In `_revoke_file`, fence matching pending/running parse jobs before selecting and revoking versions; this lock order ensures a concurrently committed processing version is visible before the exact version list is captured.
- [ ] Keep the existing publication lease fence and make revocation failures terminal rather than allowing an old parse job to retry and republish.
- [ ] Run the focused sync, authorization-loss, and publication tests.

### Task 7: Dispatch And Consume Cleanup Reliably

**Files:**
- Create: `backend/tests/integration/knowledge/test_document_cleanup.py`
- Modify: `backend/tests/integration/knowledge/test_sync_job_recovery.py`
- Modify: `backend/app/modules/knowledge/tasks.py`
- Modify: `backend/app/core/celery.py`

- [ ] Add failing tests for dispatch, periodic pending-event recovery, idempotent consumption, rollback/retry after failure, ownership mismatch, non-revoked version rejection, exact-version deletion, cross-document/cross-organization isolation, and an old delayed event not touching a newly uploaded file ID.
- [ ] Register an autoretrying cleanup Celery task and a 60-second pending cleanup-event dispatcher.
- [ ] Dispatch only valid unpublished `knowledge.document.cleanup.requested` events and update publish attempts atomically with the delivery bookkeeping.
- [ ] Consume with `ProcessedEvent` idempotency; validate event aggregate, payload UUIDs, document/source/organization ownership, and every requested version's ownership and `REVOKED` state before issuing one scoped `DELETE document_chunks WHERE document_version_id IN (...)`.
- [ ] Commit deletion and the processed marker together; on exceptions rollback so Celery retry can safely repeat.
- [ ] Run the cleanup and sync recovery tests.

### Task 8: Verify Retrieval Isolation And Re-upload Behavior

**Files:**
- Modify: `backend/tests/integration/rag/test_hybrid_permissions.py`
- Modify: `backend/tests/integration/knowledge/test_incremental_sync.py`

- [ ] Add or extend tests proving revoked versions are excluded from text/vector retrieval.
- [ ] Add a test proving the same filename with a new Drive file ID creates a separate document and ingests normally after old chunk cleanup.
- [ ] Run focused RAG permission/retrieval and knowledge sync tests.

### Task 9: Update Operational Documentation

**Files:**
- Modify: `README.md`
- Create or modify: `docs/knowledge-ingestion.md`

- [ ] Document local PDF parsing, physical page numbering, no OCR/no external upload behavior, sentence chunking, embedding validation, and existing atomic publication.
- [ ] Document configuration/install commands and a synthetic test-PDF procedure that does not call paid APIs.
- [ ] Document the supported UI workflow: delete/trash old Drive files, trigger sync, wait for cleanup, upload replacements (new Drive IDs), and sync again.
- [ ] Document rollback to the previous application version/dependencies; no schema rollback is required.

### Task 10: Full Verification And Handover

**Files:**
- Review all modified files; do not commit them.

- [ ] Run focused parser/chunker/embedding/Drive/cleanup tests.
- [ ] Run the complete backend unit test suite.
- [ ] Run the relevant integration tests when PostgreSQL is available; report infrastructure blockers exactly.
- [ ] Run Ruff and strict mypy.
- [ ] Confirm `git diff` contains no ingestion configuration fields, fingerprint logic, schema migration, unrelated file changes, credentials, or document contents.
- [ ] Report branch, modified files, event flow, exact test commands/results, unverified behavior, and UI steps.
