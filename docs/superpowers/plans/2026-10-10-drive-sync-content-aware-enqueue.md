# Drive Sync Content-Aware Enqueue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Skip duplicate parse jobs and parse outbox events when a Drive rename leaves file content unchanged.

**Architecture:** Extend the existing two-phase Drive synchronization. During the lock-free assessment, download only changed files that already map to a Document and calculate SHA-256. During the apply transaction, revalidate the source and connector, lock jobs/documents/versions in the established order, and either audit a title-only update, preserve a recoverable PROCESSING job, or enqueue normal parsing.

**Tech Stack:** Python 3.12, SQLAlchemy async, PostgreSQL, Celery JobIntent, transactional Outbox, pytest.

---

### Task 1: Specify title-only rename behavior

**Files:**
- Modify: `backend/tests/integration/knowledge/test_incremental_sync.py`

- [ ] Add a fake Drive download method and seed an existing Document with a current RETRIEVABLE version whose `content_sha256` matches the downloaded bytes.
- [ ] Change the rename regression to assert the Document ID is unchanged, the title is updated, the parse JobIntent and parse Outbox counts do not increase, and a safe audit event is written.
- [ ] Run the focused rename test and confirm it fails because synchronization still enqueues a parse job.

### Task 2: Specify PROCESSING behavior

**Files:**
- Modify: `backend/tests/integration/knowledge/test_incremental_sync.py`

- [ ] Seed a PROCESSING version and its original parse JobIntent carrying `document_version_id`.
- [ ] Rename the Drive file with identical downloaded bytes and assert no new JobIntent/outbox event is created, the original job remains recoverable, the title changes, and the version remains PROCESSING.
- [ ] Add an unsafe-recovery case whose orphan PROCESSING version produces an audit reason without being published or duplicated.
- [ ] Run the focused tests and confirm they fail for the missing enqueue decision.

### Task 3: Implement lock-free hashing and transactional decision

**Files:**
- Modify: `backend/app/modules/knowledge/sync.py`

- [ ] Add a small assessed-file value object carrying the DriveFile and optional SHA-256 for existing documents.
- [ ] In `_assess`, call `gateway.download(file_id)` and hash bytes only for authorized files already mapped to Documents; this executes after the read transaction was rolled back and before `_apply_assessment` starts.
- [ ] In `_apply_assessment`, retain the shared lock order `DriveSource -> ordered JobIntent -> Document -> ordered DocumentVersion`; revalidate ownership, source snapshot, connector, version state, and job association.
- [ ] For a matching current RETRIEVABLE version, update title/MIME and write `knowledge.document.rename.content_unchanged`, without enqueueing.
- [ ] For a matching PROCESSING version, update title/MIME and preserve its active/retryable associated job. If no safe job exists, write the same audit action with an explicit recovery reason. Never mark the version RETRIEVABLE.
- [ ] For new or changed content, preserve the existing supersession and enqueue behavior.
- [ ] Keep parsing-stage hash checks unchanged.

### Task 4: Verify regressions

**Files:**
- Test: `backend/tests/integration/knowledge/test_incremental_sync.py`
- Test: `backend/tests/integration/knowledge/test_version_publication.py`
- Test: `backend/tests/integration/knowledge/test_atomic_sync_cleanup.py`

- [ ] Run focused sync tests and verify all new tests pass.
- [ ] Run publication and cleanup regressions to verify the parsing-stage concurrency fence remains intact.
- [ ] Run Ruff, Mypy, and `git diff --check`.
- [ ] Preserve all unrelated tracked and untracked changes. Do not commit, push, or merge.
