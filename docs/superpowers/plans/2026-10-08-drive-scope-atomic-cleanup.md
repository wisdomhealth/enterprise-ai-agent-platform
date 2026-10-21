# Drive Scope Atomic Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Drive synchronization assess file and folder availability before locking, then atomically invalidate parsing, revoke versions, delete exact chunks, audit the result, and advance the cursor.

**Architecture:** Split synchronization into an external assessment phase and a database application phase. Drive metadata and scope verification complete without a database transaction; the application phase revalidates the source and authorization snapshot, then applies deterministic document actions under one shared lock order. Historical cleanup events remain supported but new synchronization cleanup is inline.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL/pgvector, Celery, Google Drive v3 client, pytest.

**Implementation status (2026-10-08):** Complete on
`feature/langchain-llamaindex-langgraph-openai`. Verification used mocked Drive
clients and an isolated PostgreSQL database. The pre-existing untracked frontend
reauthorization test still blocks the repository-wide TypeScript check; see the
handover results rather than treating that unrelated check as passed.

---

### Task 1: Drive evidence and error classification

**Files:**
- Modify: `backend/tests/unit/knowledge/test_drive_gateway.py`
- Modify: `backend/app/modules/knowledge/drive_gateway.py`

- [x] Add failing tests for trashed, removed-only, file-level 404, explicit file access denial, OAuth, quota/rate-limit, timeout, and Google 5xx behavior.
- [x] Run the focused unit tests and confirm failures are caused by missing evidence classification.
- [x] Preserve `trashed` separately from `removed`, expose specific-file unavailability, and parse Google error reasons without treating all 403 responses alike.
- [x] Run the focused unit tests until green.

### Task 2: Two-phase scope assessment

**Files:**
- Modify: `backend/tests/integration/knowledge/test_authorization_loss.py`
- Modify: `backend/tests/integration/knowledge/test_incremental_sync.py`
- Modify: `backend/app/modules/knowledge/sync.py`

- [x] Add failing tests for root removal without child events, multi-level subfolder loss, moved-within-root preservation, moved-out cleanup, uncertain-scope preservation on transient failure, and refreshed descendant IDs.
- [x] Confirm the tests fail against the current direct-parent/stale-descendant implementation.
- [x] Read the source snapshot and close its transaction before Drive calls; refresh root/descendants and individually verify imported documents when folder events make relationships uncertain.
- [x] Reopen a transaction, lock/revalidate organization, status, root, include-descendants, allowed descendants, cursor, and connector authorization identity before applying results.
- [x] Use `DISABLED` only for confirmed root unavailability; retain `ACTIVE` on temporary failures and avoid an unconditional final status assignment.

### Task 3: Atomic revocation, cleanup, and audit

**Files:**
- Modify: `backend/tests/integration/knowledge/test_authorization_loss.py`
- Modify: `backend/app/modules/knowledge/sync.py`
- Add: `backend/app/modules/knowledge/actors.py`

- [x] Add failing assertions for physical chunk deletion in the same transaction, already-revoked versions with chunks, exact audit reason/version IDs/count, ownership isolation, idempotency, and rollback on cleanup/audit/commit failure.
- [x] Implement one caller-owned transactional revocation helper using stable ordering and exact organization/source/document/version predicates.
- [x] Invalidate all pending/running parse jobs, clear current versions, revoke nonterminal versions, delete chunks for all revoked target versions, and write one system-actor audit record per changed document.
- [x] Stop emitting new `knowledge.document.cleanup.requested` events from synchronization.
- [x] Confirm cursor advancement, audit, revocation, deletion, and task invalidation commit or roll back together.

### Task 4: Shared parsing guard and lock order

**Files:**
- Modify: `backend/tests/integration/knowledge/test_version_publication.py`
- Modify: `backend/app/modules/knowledge/ingestion.py`
- Modify: `backend/app/modules/rag/embeddings.py`
- Modify: `backend/app/modules/knowledge/sync.py`

- [x] Add barrier-based failing tests for parse-first and cleanup-first orderings, multiple parse tasks, checkpoint loss, embedding persistence loss, and publication loss.
- [x] Move PDF parsing/chunk computation and embedding provider calls outside write-lock transactions.
- [x] Use the common order: source, connector when relevant, parse jobs by ID,
  document, versions by ID, chunks by ordinal/ID. Paths that do not touch an
  earlier resource preserve the same relative order for the locks they share.
- [x] Validate lease owner, expiry, task generation, document/source binding, and version state in the same transaction as each persistence stage.
- [x] Prevent enqueue after an unavailable decision in the same batch and prevent historical work from affecting a new Drive ID.

### Task 5: Historical cleanup compatibility and worker behavior

**Files:**
- Modify: `backend/tests/integration/knowledge/test_document_cleanup.py` only if it can be changed without overwriting unrelated untracked work; otherwise add a new tracked test module.
- Modify: `backend/tests/integration/knowledge/test_sync_job_recovery.py`
- Modify: `backend/app/modules/knowledge/tasks.py`
- Modify: `backend/app/core/celery.py` only if obsolete UI-facing scheduling must be removed.

- [x] Test historical event ownership validation, stable lock order, already-absent chunks, and isolation from newly uploaded same-name/new-ID documents.
- [x] Keep the historical consumer and retry sweep safe and idempotent.
- [x] Remove new-sync cleanup dispatching and cleanup event IDs while retaining other Outbox workflows.
- [x] Ensure sync success is recorded only after the atomic application transaction commits.

### Task 6: Operational status, logs, documentation, and UI contract

**Files:**
- Modify: `backend/app/modules/knowledge/operations.py`
- Modify: `backend/app/modules/knowledge/tasks.py`
- Modify: `docs/runbooks/drive-sync.md`
- Modify frontend only if an existing component exposes cleanup-pending or falsely reports page completion.

- [x] Add structured operational logs for assessment failure, stale result rejection, rollback, and retry without credentials or document contents.
- [x] Record root-unavailable reason in audit and sync job error information, distinct from OAuth and administrator disablement.
- [x] Document restoring operation by reconfiguring/validating the root scope, then manually syncing; never automatically restore `ACTIVE` or start ingestion.
- [x] Confirm the UI has no separate cleanup-pending state and describes accepted/enqueued synchronization accurately.

### Task 7: Full verification

**Files:**
- Review all changed files; do not commit, push, merge, switch branches, or modify real Drive/database data.

- [x] Run new tests first and retain red/green evidence.
- [x] Run knowledge unit tests, focused PostgreSQL integration tests, and concurrency tests against an isolated database.
- [x] Run the knowledge unit suite excluding the pre-existing untracked
  reauthorization test that cannot import its requested symbol; run Ruff,
  strict mypy, documentation checks, and relevant frontend tests. Record the
  separate pre-existing TypeScript test signature error as blocked.
- [x] Run `git diff --check` and verify unrelated/untracked files remain preserved.
- [x] Report exact commands/results and explicitly list any tests blocked by the environment.
