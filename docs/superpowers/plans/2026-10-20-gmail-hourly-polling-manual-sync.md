# Gmail Hourly Polling and Manual Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add independent hourly Gmail polling and administrator manual synchronization through the existing durable history pipeline.

**Architecture:** A shared coordinator owns the `EmailSyncState` lock and atomically creates or deduplicates history jobs for scheduled, manual, and push triggers. Celery Beat scans every minute, but mailbox eligibility is based only on the last successful synchronization and the reused reconciliation interval.

**Tech Stack:** FastAPI, SQLAlchemy async, PostgreSQL, Celery, transactional Outbox, Next.js, React, Vitest, pytest.

---

### Task 1: Configuration and scheduler contract

**Files:**
- Modify: `backend/tests/unit/email/test_push_config.py`
- Modify: `backend/tests/integration/email/test_tasks.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/core/celery.py`
- Modify: `compose.yaml`
- Modify: `.env.example`

- [x] Add failing tests for independent polling defaults and the one-minute scan entry.
- [x] Run the focused tests and verify the expected configuration failures.
- [x] Add `GMAIL_POLLING_ENABLED`; reuse `GMAIL_RECONCILIATION_INTERVAL_SECONDS` as the due interval.
- [x] Make the existing reconciliation Beat entry scan every 60 seconds without adding another entry.
- [x] Re-run focused tests.

### Task 2: Shared synchronization coordinator

**Files:**
- Create: `backend/app/modules/email/sync.py`
- Modify: `backend/app/modules/email/push.py`
- Modify: `backend/app/modules/email/recovery.py`
- Modify: `backend/app/modules/email/tasks.py`
- Test: `backend/tests/integration/email/test_gmail_push.py`
- Test: `backend/tests/integration/email/test_gmail_recovery_management.py`
- Test: `backend/tests/integration/email/test_tasks.py`

- [x] Add failing tests proving manual, scheduled, and push requests share one effective active job.
- [x] Add failing tests for not-due, paused, recovery-required, recovery-truncated, and stale terminal references.
- [x] Verify the tests fail because scheduling is duplicated or unconditional.
- [x] Implement the coordinator with row locking, active-job validation, generation fencing, JobIntent, and Outbox creation.
- [x] Route all three triggers through the coordinator and preserve Push receipt/high-water persistence.
- [x] Re-run focused tests.

### Task 3: Successful completion baseline

**Files:**
- Modify: `backend/tests/integration/email/test_tasks.py`
- Modify: `backend/tests/integration/email/test_gmail_ingestion.py`
- Modify: `backend/app/modules/email/tasks.py`
- Modify: `backend/app/modules/email/ingestion.py`

- [x] Add failing tests proving only successful and empty successful history jobs update `last_success_at`.
- [x] Add regression assertions that failed classification/draft jobs are untouched.
- [x] Verify failure before implementation.
- [x] Finalize successful history jobs without enqueueing an empty follow-up.
- [x] Preserve cursor commit ordering, initialization, pagination, expiry recovery, and retry behavior.
- [x] Re-run ingestion and task tests.

### Task 4: Manual API status and authorization

**Files:**
- Modify: `backend/tests/integration/email/test_gmail_recovery_management.py`
- Modify: `backend/tests/integration/operations/test_safe_manual_retry.py`
- Modify: `backend/app/modules/operations/schemas.py`
- Modify: `backend/app/modules/operations/router.py`
- Modify: `backend/app/modules/email/recovery.py`

- [x] Add failing route tests for Push-disabled acceptance, duplicate active jobs, reviewer rejection, and cross-organization rejection.
- [x] Verify the tests fail on response status details or duplicate conflict behavior.
- [x] Extend the existing response with active job ID/state/trigger and last success time.
- [x] Keep the existing administrator-only endpoint and recovery restrictions.
- [x] Re-run operations and recovery tests.

### Task 5: Administrator email-page control

**Files:**
- Modify: `frontend/app/staff/email/page.tsx`
- Modify: `frontend/components/email/EmailQueue.tsx`
- Modify: `frontend/components/admin/GmailPushManagement.tsx`
- Modify: `frontend/lib/staff-api.ts`
- Test: `frontend/tests/email-review.test.tsx`

- [x] Add failing tests for admin-only rendering, repeated-click prevention, active-job display, completion polling, recovery guidance, and queue refresh.
- [x] Verify the focused Vitest test fails.
- [x] Implement the minimal control using the existing admin endpoints.
- [x] Refresh the queue only after observed successful task completion.
- [x] Re-run focused frontend tests.

### Task 6: Documentation and verification

**Files:**
- Modify: `docs/runbooks/gmail-push.md`
- Modify: `README.md`

- [x] Document independent Push and polling configuration and the reused reconciliation interval.
- [x] Document deployment restart and manual verification steps.
- [x] Run backend focused suites against the isolated database.
- [x] Run frontend typecheck, lint, tests, and production build.
- [x] Run `git diff --check` and inspect the final diff without committing.
