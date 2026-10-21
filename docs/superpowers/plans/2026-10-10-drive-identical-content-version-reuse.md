# Drive Identical-Content Version Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse identical retrievable document versions and atomically replace and clean the prior current version when Drive content changes.

**Architecture:** Hash immediately after download, then use a short source/job/document/version/chunk locked transaction for reuse or state rejection. Parse and embed without write locks; revalidate under the same lock order before checkpoint insertion and final atomic publication/replacement.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL/pgvector, Celery job intents, pytest.

---

### Task 1: Freeze synchronization identity and rename behavior

**Files:**
- Modify: `backend/tests/integration/knowledge/test_incremental_sync.py`

- [ ] Add a test that syncs one Drive ID twice with different names and verifies one document, unchanged document ID, latest title, and a successor parse job.
- [ ] Run `pytest tests/integration/knowledge/test_incremental_sync.py -q`; verify the rename test passes on existing `_upsert_document` behavior.

### Task 2: Reproduce identical-content reuse and state handling

**Files:**
- Modify: `backend/tests/integration/knowledge/test_version_publication.py`
- Modify: `backend/app/modules/knowledge/ingestion.py`

- [ ] Add counting parser and embedding fakes and a test where a new job downloads bytes matching a `RETRIEVABLE` version; assert version/chunk counts unchanged, current version reused, job succeeded, and parser/embed calls remain zero.
- [ ] Run the test and verify RED at duplicate-version handling.
- [ ] Add parametrized tests for another job's `PROCESSING`, plus `FAILED`, `REVOKED`, and `DELETED`; assert state-specific `DocumentParseError.code`, terminal job state, and no parser/embed calls.
- [ ] Run the tests and verify RED.
- [ ] In `parse`, calculate `sha256(content).hexdigest()` before `_prepare_version`; add a locked lookup scoped to the current document and validate the matching state.
- [ ] Associate reusable version IDs with the current job and complete the job in the same transaction; preserve the existing checkpoint path only when the current job already references its `PROCESSING` version.
- [ ] Run the focused tests and verify GREEN.

### Task 3: Close the concurrent insert window with shared lock order

**Files:**
- Modify: `backend/tests/integration/knowledge/test_version_publication.py`
- Modify: `backend/app/modules/knowledge/ingestion.py`

- [ ] Add a two-session test in which identical tasks pass download/hash assessment concurrently; verify one version is created and the loser either reuses the retrievable winner or terminates once with `DOCUMENT_CONTENT_PROCESSING`, never an integrity-error retry loop.
- [ ] Run the test and verify RED.
- [ ] Refactor the parse checkpoint to lock source, all relevant parse jobs ordered by ID, document, versions ordered by ID, then chunks; validate lease generation, source status/scope, ownership, and task supersession.
- [ ] Repeat the matching-hash lookup after parsing and before insertion. Do not catch-and-query inside a failed transaction; prevent the conflicting insert instead.
- [ ] Run focused concurrency and existing lease-fencing tests and verify GREEN.

### Task 4: Publish replacement and clean old chunks atomically

**Files:**
- Modify: `backend/tests/integration/knowledge/test_version_publication.py`
- Modify: `backend/app/modules/rag/embeddings.py`
- Modify: `backend/app/modules/knowledge/ingestion.py`

- [ ] Add a changed-content test asserting the previous current version remains retrievable during parser/embed work, then becomes `REVOKED`, loses only its chunks, and the new version becomes current/retrievable with embedded chunks.
- [ ] Add a same-current-version reuse test asserting no chunk deletion.
- [ ] Add audit assertions for `knowledge.document.version.replaced` with organization, source, document, job, old/new version IDs, and deleted count.
- [ ] Run tests and verify RED.
- [ ] Extend embedding publication so final locking includes all document versions/chunks in stable order and permits an ingestion transition callback before flush.
- [ ] Implement the transition: read the actual locked `current_version_id`; if different, switch current, revoke only that version, delete only its chunks, and record the worker audit event. Complete the job before the single commit.
- [ ] Run focused publication tests and verify GREEN.

### Task 5: Prove rollback and stale-task protection

**Files:**
- Modify: `backend/tests/integration/knowledge/test_version_publication.py`

- [ ] Add parser and embedding failure tests proving old current/chunks remain unchanged.
- [ ] Inject cleanup/audit/flush failure and verify new publication, current switch, old revocation/deletion, audit, and job completion all roll back.
- [ ] Add a late older-task test proving it cannot change `current_version_id` or recreate deleted chunks after a newer task completes.
- [ ] Run each test RED before implementation adjustments, then GREEN after minimal fixes.

### Task 6: Freeze retrieval and isolation guarantees

**Files:**
- Modify: `backend/tests/integration/rag/test_retriever.py`
- Modify: `backend/tests/integration/knowledge/test_version_publication.py`

- [ ] Add retrieval coverage with old and new versions present; assert only current `RETRIEVABLE` chunks participate in vector and full-text candidate assembly.
- [ ] Add cross-document and cross-organization identical-hash tests proving no reuse or deletion crosses ownership boundaries.
- [ ] Add business-row preservation coverage for stored chat/email citation payloads when old chunks are deleted.
- [ ] Run focused tests and verify GREEN; change retrieval code only if a test exposes a missing current/state/scope predicate.

### Task 7: Verification and handover

**Files:**
- Modify if necessary: `docs/architecture/knowledge-ingestion.md`

- [ ] Run Ruff on changed Python files.
- [ ] Run focused knowledge and RAG tests against the isolated database.
- [ ] Run the existing lease, cleanup, authorization-loss, and retriever regression suites.
- [ ] Inspect `git diff --check` and confirm `backend/app/core/celery.py` and unrelated/untracked files were not modified.
- [ ] Report state behavior, atomic boundaries, audit query (`audit_events` filtered by organization/action/object), exact test results, UI steps, and the revoked-hash restoration limitation.
