# Gmail Push, Draft Reliability, Homepage, and Access Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user explicitly prohibited automatic commits, pushes, and merges, so this plan intentionally contains no commit steps.

**Goal:** Deliver authenticated push-driven Gmail ingestion, canonical bounded draft retries, application access management, and the six-link public homepage without weakening existing authorization or delivery controls.

**Architecture:** Extend `EmailSyncState` and `JobIntent` rather than creating parallel schedulers, use one receipt table and existing Outbox/lease services for durable push work, and fence all lifecycle operations with database generations. Split drafting into external preparation plus one atomic finalization transaction, and layer application capability grants over existing role/resource/operation checks.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL, Alembic, Celery, Google Gmail API, Google Auth JWT verification, Next.js 15, React 19, TypeScript, pytest, Vitest, Playwright.

## Implementation Status (2026-10-19)

The approved code scope is implemented in this feature worktree: authenticated Gmail push receipt,
transactional receipt/JobIntent/Outbox persistence, Outbox-to-Celery dispatch, leased history
consumption, bounded H0 recovery, watch lifecycle fencing, draft canonical-job/retry-cycle repair,
application access, server-side route gates, migration/backfill, homepage, administration controls,
and operator documentation. The detailed checkboxes below are retained as the original test-first
execution record; this status section and the final verification report describe the resulting
implementation.

Local verification covers focused backend integration/unit suites, migration upgrade/downgrade,
frontend Vitest/type checking, and a production frontend build. A real Google/Pub/Sub target must
still verify the authenticated receiver through Worker consumption before deployment replaces the
running version. The initial-organization/administrator bootstrap CLI remains the explicitly
deferred next task and no bootstrap example command is executable in this change.

---

## File Map

**Backend data and configuration**

- Create `backend/alembic/versions/0022_gmail_push_access_draft_reliability.py`: cursor rename, sync/watch fields, receipt table, canonical draft pointer, retry-cycle fields, indexes, grants, and safe historical backfill.
- Modify `backend/app/core/config.py`: typed Gmail push, recovery, receipt, draft retry, and public-status settings.
- Modify `backend/app/modules/email/models.py`: sync/watch enums and fields, receipt model, canonical draft job.
- Modify `backend/app/modules/jobs/models.py`: persistent retry-cycle metadata.

**Gmail push and synchronization**

- Modify `backend/app/modules/email/gmail_gateway.py`: profile, watch, stop, bounded recovery, and typed provider errors.
- Create `backend/app/modules/email/push.py`: Pub/Sub envelope/JWT validation and transactional receipt ingestion.
- Create `backend/app/modules/email/push_router.py`: short unauthenticated-at-session-level but OIDC-authenticated webhook route.
- Create `backend/app/modules/email/watch.py`: generation-fenced Start, Stop, Renew, Resume, manual sync, and compensation.
- Modify `backend/app/modules/email/ingestion.py`: fixed-generation cursor processing, page-token restart, recovery, and cursor finalization.
- Modify `backend/app/modules/email/tasks.py`: canonical sync execution, renewal/reconciliation/cleanup tasks, and removal of poll producer.
- Modify `backend/app/modules/outbox/dispatcher.py`: reuse the existing at-least-once dispatch contract where a shared helper is sufficient; keep Email event validation/mapping in the Email task module.
- Modify `backend/app/core/celery.py`: remove one-minute Gmail poll; add daily renewal, six-hour reconciliation, and receipt cleanup.
- Modify `backend/app/main.py`: register the inbound Gmail push router and injectable services.

**Draft reliability**

- Modify `backend/app/modules/email/drafting.py`: pure draft preparation and typed failure classification.
- Modify `backend/app/modules/email/tasks.py`: atomic success/failure finalization on the original leased job.
- Modify `backend/app/modules/email/review.py`: atomic Regenerate canonical replacement.
- Modify `backend/app/modules/operations/service.py` and `backend/app/modules/operations/router.py`: authorized finite manual retry cycles.

**Authorization and operations**

- Create `backend/app/modules/authorization/application.py`: capability resource IDs and effective access checks.
- Modify `backend/app/modules/authorization/types.py`: register the `application` resource and `application.access` action.
- Modify `backend/app/modules/authorization/policy.py`: add ADMIN/REVIEWER application action/state rules while retaining grant and organization checks.
- Create `backend/app/modules/operations/application_authorization.py`: list/replace application grants using `ResourceGrant`.
- Modify `backend/app/modules/operations/schemas.py` and `backend/app/modules/operations/router.py`: effective access and Gmail push status/action APIs.
- Modify Email, Support, RAG, review, delivery, and operations routers only where needed to add the coarse application gate while retaining every existing fine-grained dependency.

**Frontend**

- Modify `frontend/app/page.tsx` and `frontend/app/globals.css`: public responsive homepage and six required destinations.
- Create `frontend/lib/application-access.ts`: server-side effective access loader/guards.
- Create `frontend/app/staff/email/layout.tsx`, `frontend/app/staff/support/layout.tsx`, and `frontend/app/staff/admin/layout.tsx`: page-boundary guards.
- Create `frontend/components/admin/ApplicationAccessManagement.tsx` and `frontend/components/admin/GmailPushManagement.tsx`.
- Modify `frontend/app/staff/admin/authorization/page.tsx` and `frontend/lib/staff-api.ts`.

**Documentation**

- Create `docs/runbooks/gmail-push.md`.
- Modify `README.md`, `.env.example` if present, deployment Compose environment wiring, and existing staff-auth/email-triage runbooks.

## Task 1: Freeze Current Root Causes and Configuration Defaults

**Files:**
- Modify: `backend/tests/integration/email/test_tasks.py`
- Modify: `backend/tests/unit/core/test_celery_database.py`
- Create: `backend/tests/unit/email/test_push_config.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/core/celery.py`

- [x] Add a regression proving completed unchanged history jobs are not regenerated by any scheduled one-minute producer. Assert `gmail-history-poll` is absent while `email-intent-dispatch`, delivery Outbox dispatch, lease recovery, retention, and unrelated schedules remain.

```python
def test_gmail_push_replaces_one_minute_poll_without_disabling_recovery() -> None:
    schedule = create_celery(Settings()).conf.beat_schedule
    assert "gmail-history-poll" not in schedule
    assert "email-history-outbox-dispatch" in schedule
    assert "email-intent-dispatch" in schedule
    assert "email-delivery-outbox-dispatch" in schedule
    assert "retention-job-recovery" in schedule
```

- [x] Add configuration tests proving `GMAIL_PUSH_ENABLED` defaults to `False`, `GMAIL_RECONCILIATION_INTERVAL_SECONDS` defaults to six hours, receipt retention to 14 days, recovery to 7 days/500 messages, and draft attempts to 3 including the first execution.
- [x] Run `cd backend && pytest tests/unit/email/test_push_config.py tests/unit/core/test_celery_database.py -q`; verify the new assertions fail for missing settings and the old poll schedule.
- [x] Add strict `Settings` fields and remove only the `gmail-history-poll` beat entry. Register a one-minute `email-history-outbox-dispatch` sweep for committed unpublished events, plus renewal/reconciliation/cleanup settings guarded inside their task bodies by push configuration. The Outbox sweep publishes existing jobs; it never creates a Gmail history job or polls Gmail.
- [x] Run the unit command plus `tests/integration/email/test_tasks.py`; verify 17 tests pass and all retained schedules are unchanged.

## Task 2: Add the Durable Schema and Safe Backfill

**Files:**
- Create: `backend/alembic/versions/0022_gmail_push_access_draft_reliability.py`
- Modify: `backend/app/modules/email/models.py`
- Modify: `backend/app/modules/jobs/models.py`
- Modify: `backend/tests/integration/email/test_email_migration.py`
- Modify: `backend/tests/integration/authorization/test_authorization_migration.py`

- [ ] Add migration tests that insert an existing sync cursor, completed draft, one unambiguous live draft job, multiple ambiguous live jobs, terminal jobs, ADMIN/REVIEWER/MEMBER users, and knowledge grants before upgrading.
- [ ] Assert after upgrade:
  - `history_id` value is preserved as `processed_history_id`;
  - mailbox uniqueness is global;
  - completed drafts have no active job;
  - one relationship-consistent live job becomes canonical;
  - ambiguous work becomes `FAILED_TERMINAL/EMAIL_DRAFT_CANONICAL_JOB_AMBIGUOUS`;
  - terminal jobs remain terminal;
  - application grants are added only for same-organization active ADMIN/REVIEWER users who have effective pre-change `knowledge.review` access to at least one active knowledge base, including any actual ADMIN implicit access in `AuthorizationService`;
  - each qualifying legacy user receives both Email and Support application grants to preserve the two prior entry surfaces;
  - no knowledge scope is added and MEMBER gains nothing.
- [ ] Run `cd backend && pytest tests/integration/email/test_email_migration.py tests/integration/authorization/test_authorization_migration.py -q`; verify RED.
- [ ] Implement enum types and columns. Use nullable cycle fields for legacy jobs, and initialize new draft jobs explicitly:

```python
class GmailWatchStatus(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    ERROR = "ERROR"

class GmailSyncStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED_ERROR = "PAUSED_ERROR"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERY_RUNNING = "RECOVERY_RUNNING"
    RECOVERY_TRUNCATED = "RECOVERY_TRUNCATED"
```

- [ ] Create `gmail_push_receipts` with unique `(subscription_name, pubsub_message_id)`, indexed expiry, and least-privilege application grants. Rename the cursor with `ALTER TABLE ... RENAME COLUMN` so values are not copied or lost.
- [ ] Backfill canonical jobs using `EmailStateHistory.job_id`, `JobIntent.payload->>'work_item_id'`, work-item state, current draft, and job state. Safely terminate only clearly stale nonterminal jobs with `EMAIL_DRAFT_SUPERSEDED`; preserve succeeded and terminal jobs unchanged. Never choose solely from the `:vN` suffix, never delete task/audit history, and send ambiguous work items to `FAILED_TERMINAL/EMAIL_DRAFT_CANONICAL_JOB_AMBIGUOUS`.
- [ ] Backfill application grants by reproducing the pre-change effective policy in the migration query/helper, not by testing only for a literal grant row. Do not add or widen any `knowledge.review` grant.
- [ ] Add `attempt_cycle`, `cycle_start_attempts`, and `cycle_attempt_limit` to `JobIntent`; leave legacy values nullable until canonical reconciliation or authorized retry sets them.
- [ ] Run the focused migrations plus `alembic upgrade head`, downgrade to `0021_webhook_subscriptions`, and upgrade again; verify data preservation and schema round trip.

## Task 3: Extend the Gmail Gateway with Typed Lifecycle Operations

**Files:**
- Modify: `backend/app/modules/email/gmail_gateway.py`
- Modify: `backend/tests/unit/email/test_gmail_gateway.py`

- [ ] Write fake Google API tests for `getProfile`, `users.watch`, `users.stop`, fixed-start history pagination, bounded message listing, and these distinct errors: invalid page token, expired history cursor, temporary throttle, authorization/configuration, and quota/billing.
- [ ] Assert `users.watch` receives exactly `body={"topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "include"}`. Assert `users.history.list` receives `startHistoryId`, `historyTypes=["messageAdded"]`, `labelId="INBOX"`, and `pageToken` only when present; separately assert parsing of the response's `messagesAdded` collection.
- [ ] Define concrete gateway contracts:

```python
@dataclass(frozen=True, slots=True)
class GmailProfile:
    email_address: str
    history_id: str

@dataclass(frozen=True, slots=True)
class GmailWatchResult:
    history_id: str
    expiration: datetime

async def profile(self) -> GmailProfile: ...
async def start_watch(self, topic_name: str) -> GmailWatchResult: ...
async def stop_watch(self) -> None: ...
async def list_recent_inbox(self, *, after: datetime, page_token: str | None, max_results: int) -> GmailMessagePage: ...
```

- [ ] Run `cd backend && pytest tests/unit/email/test_gmail_gateway.py -q`; verify RED.
- [ ] Implement the methods with `asyncio.to_thread`, exact Gmail request fields, numeric history validation, email normalization limited to trim/lowercase, and provider-status mapping. Preserve `historyTypes=["messageAdded"]` and `labelId="INBOX"`.
- [ ] Run the unit file; verify GREEN without network access.

## Task 4: Validate Pub/Sub Identity Before Tenant Resolution

**Files:**
- Create: `backend/app/modules/email/push.py`
- Create: `backend/app/modules/email/push_router.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/unit/email/test_gmail_push_auth.py`

- [ ] Write table-driven tests rejecting bad JWT signature, issuer, expiration, audience, subject/email claim, `email_verified`, service-account email, subscription, body size, base64, JSON, mailbox, and nonnumeric history ID. Assert no database lookup occurs before token and subscription validation.
- [ ] Add a valid-envelope test returning this internal value:

```python
@dataclass(frozen=True, slots=True)
class VerifiedGmailNotification:
    subscription_name: str
    pubsub_message_id: str
    published_at: datetime | None
    mailbox_address: str
    history_id: str
```

- [ ] Run `cd backend && pytest tests/unit/email/test_gmail_push_auth.py -q`; verify RED.
- [ ] Implement JWT verification with Google's existing auth dependency, exact configured audience and service-account match, and a body read capped by `GMAIL_PUSH_MAX_BODY_BYTES`. Do not accept organization or connector identifiers from the envelope.
- [ ] Register `POST /api/v1/webhooks/gmail/pubsub`; keep staff session dependencies off this route because OIDC is its authentication boundary.
- [ ] Run the unit test and a router smoke test; verify valid notification reaches the service and invalid requests return 400/401/403 without tenant disclosure.

## Task 5: Persist Receipts, Pending Work, Job, and Outbox Atomically

**Files:**
- Modify: `backend/app/modules/email/push.py`
- Modify: `backend/app/modules/email/tasks.py`
- Modify: `backend/app/modules/outbox/dispatcher.py`
- Modify: `backend/app/core/celery.py`
- Create: `backend/tests/integration/email/test_gmail_push.py`
- Create: `backend/tests/integration/email/test_gmail_history_outbox.py`

- [ ] Add integration tests for valid push, duplicate receipt, out-of-order history, old history, receipt cleaned then old redelivery, cross-subscription identical message IDs, unknown mailbox, disabled automatic sync, and database failure before commit.
- [ ] Add a transaction race test in which two notifications for one mailbox arrive concurrently and assert one active sync job, numeric maximum pending history, one Outbox request, and both receipts.
- [ ] Add dispatcher tests proving:
  - `email.gmail_history.requested` maps to `email_job.delay(str(event.aggregate_id))` only when aggregate type, job kind, and payload identity match;
  - successful publish increments attempts and then sets `published_at`;
  - broker failure increments attempts, leaves `published_at=NULL`, and remains sweepable;
  - duplicate delivery after broker success/pre-update crash reaches the same job ID and is absorbed by the `JobIntent` lease;
  - a pending JobIntent with a valid unpublished history Outbox event is not also published by the pending-job recovery scan;
  - a legacy pending history job without a usable Outbox event remains recoverable by the fallback scan.
- [ ] Use integer comparison explicitly in expected behavior:

```python
assert int(state.pending_history_id) == max(int(first), int(second))
assert state.active_sync_job_id == jobs[0].id
```

- [ ] Run `cd backend && pytest tests/integration/email/test_gmail_push.py -q`; verify RED.
- [ ] Implement one transaction with lock order `EmailSyncState -> JobIntent -> Outbox/Receipt`. Validate trusted mailbox mapping before inserting the receipt. On duplicate receipt return success; on `history <= processed` or `auto_sync_enabled=false`, record receipt/diagnostics but do not set pending or enqueue. New work writes receipt, `JobIntent`, and `email.gmail_history.requested` Outbox event before the webhook can return 204.
- [ ] For new work, update pending by numeric max and create `JobIntent + Outbox + active_sync_job_id` together. Return 204 only after commit; rollback and return non-2xx on persistence failure.
- [ ] Extend the existing Email dispatcher rather than adding a new dispatch framework. Publish first, set `published_at` only after success, and allow at-least-once duplicates. Coordinate `_dispatch_pending_email_jobs` so it skips jobs with a valid unpublished history event and serves only legacy missing-Outbox plus lease-recovery cases.
- [ ] Register `dispatch_pending_email_history_outbox_events` in Celery beat. Run both integration files repeatedly; verify races, publish failure, duplicate delivery, and crash-window behavior are stable.

## Task 6: Implement Generation-Fenced Start, Stop, and Renew

**Files:**
- Create: `backend/app/modules/email/watch.py`
- Modify: `backend/app/modules/operations/schemas.py`
- Modify: `backend/app/modules/operations/router.py`
- Create: `backend/tests/integration/email/test_gmail_watch.py`

- [ ] Add tests for Start's two transactions, immediate notification before watch completion, Stop racing a delayed Start, Stop racing Renew, renew failure while the prior watch remains unexpired, external success/local save failure, and stopped reconciliation suppression.
- [ ] Assert network fakes observe no held write transaction. Assert delayed completions fail their generation compare-and-set and cannot change `auto_sync_enabled=false`.
- [ ] Run `cd backend && pytest tests/integration/email/test_gmail_watch.py -q`; verify RED.
- [ ] Implement lifecycle reservations:

```python
@dataclass(frozen=True, slots=True)
class WatchOperation:
    connector_id: UUID
    generation: int
    action: Literal["START", "STOP", "RENEW"]
```

- [ ] For Start, obtain profile, then commit trusted mailbox + enabled intent + `STARTING` generation before `users.watch`. For Stop, commit disabled intent + `STOPPING` generation before `users.stop`. For Renew, reserve a generation only when enabled.
- [ ] Complete each operation only when the stored generation still matches. On external success/local failure leave recoverable `STARTING`/error diagnostics for reconciliation. Do not advance processed or pending cursor from watch results.
- [ ] Add administrator endpoints using existing idempotency and audit patterns. Verify operator remains an active same-organization ADMIN inside each mutation transaction.
- [ ] Run the watch tests; verify immediate push is retained, Stop wins every race, and renewal error does not suppress valid notification work.

## Task 7: Rework History Processing Around a Fixed Start Cursor

**Files:**
- Modify: `backend/app/modules/email/ingestion.py`
- Modify: `backend/app/modules/email/tasks.py`
- Modify: `backend/tests/integration/email/test_gmail_ingestion.py`
- Modify: `backend/tests/integration/email/test_ingestion_recovery.py`
- Create: `backend/tests/integration/email/test_gmail_sync_concurrency.py`

- [ ] Add tests proving one immutable `sync_start_history_id` across pages, page-token generation ownership, safe token restart, no cursor advance on partial failure, final cursor from Gmail's completed response, and classification/model failure after durable ingestion not blocking cursor advance.
- [ ] Add a guard test proving normal synchronization never calls `history.list` when `processed_history_id` is `NULL`; it must create/resume one initialization recovery batch instead.
- [ ] Add races for notification during finalization, expired lease reclaiming the same job, two workers, terminal failure, exhausted retries, Resume, and Stop during a running job.
- [ ] Run the three focused files; verify RED.
- [ ] Split each worker pass into external fetch and short locked persistence. Use `sync_generation` and `active_sync_job_id` fences on every page. Persist eligible work items, classification intents, Outbox records, and next token in the same page transaction.
- [ ] On invalid page token, clear only the matching generation token and restart from `sync_start_history_id`. On expired cursor, enter `RECOVERY_REQUIRED`; do not classify every pagination error as cursor expiry.
- [ ] Finalize with Gmail's final response history ID, never pending high-water. If a newer pending value remains and automatic sync is enabled, create exactly one next generation atomically. If disabled, do not create follow-up automatic work.
- [ ] On nonretryable/exhausted failure, clear the active reference, preserve pending, set `PAUSED_ERROR`, and suppress automatic replacement until authorized Resume.
- [ ] Run all focused tests and existing lease fencing tests; verify GREEN.

## Task 8: Add Bounded Recovery, Reconciliation, and Receipt Cleanup

**Files:**
- Modify: `backend/app/modules/email/ingestion.py`
- Modify: `backend/app/modules/email/tasks.py`
- Modify: `backend/app/core/celery.py`
- Modify: `backend/app/modules/operations/router.py`
- Modify: `backend/app/modules/operations/schemas.py`
- Create: `backend/tests/integration/email/test_gmail_recovery.py`

- [ ] Add tests for first enablement with `processed_history_id=NULL`: `H0` is obtained before scanning, batch ID/H0/window/page token/count/status are persisted, no `history.list(None)` occurs, a crash resumes the same page/batch, notifications update pending without spawning a normal competitor, and the first processed cursor appears only after bounded scan plus `H0` catch-up.
- [ ] Add tests for expired-cursor recovery: arrivals during scan are caught after `H0`, baseline expiry/failure remains recoverable, 7-day/500-message defaults apply, truncation with more pages reports `RECOVERY_TRUNCATED`, Continue works, and confirmed/audited Accept limited recovery still performs mandatory `H0` catch-up.
- [ ] Add an explicit “only after enablement” option test requiring administrator confirmation, expected version, and audit; assert it is never selected by default.
- [ ] Add tests proving scheduled reconciliation skips `auto_sync_enabled=false`, never recreates a 60-second poll, and coalesces with an existing active sync job. Add receipt cleanup batch/retention tests.
- [ ] Run `cd backend && pytest tests/integration/email/test_gmail_recovery.py -q`; verify RED.
- [ ] Implement one initialization/recovery state machine with persisted `recovery_batch_id`, `recovery_baseline_history_id`, `recovery_window_started_at`, `recovery_page_token`, `recovery_processed_count`, `recovery_has_more`, and status. Never set `processed_history_id` to the latest profile value without completing history from `H0`.
- [ ] Implement daily watch renewal, configurable six-hour reconciliation, and bounded receipt cleanup tasks. Keep Outbox, pending job dispatch, retries, and other schedules untouched.
- [ ] Implement explicit confirmation schema containing recovery batch ID, window start, processed count, truncation flag, and expected version. Audit the accepted risk and continue `H0` catch-up.
- [ ] Run focused recovery and schedule tests; verify GREEN.

## Task 9: Turn Draft Generation into Pure Preparation

**Files:**
- Modify: `backend/app/modules/email/drafting.py`
- Create: `backend/tests/unit/email/test_draft_failures.py`
- Modify: `backend/tests/integration/email/test_draft_generation.py`

- [ ] Add tests that `EmailDraftingService.prepare` performs no work-item/job/outbox/audit mutations and returns a prepared value or raises structured `EmailDraftFailure`.
- [ ] Cover temporary throttle, quota/billing, invalid credentials/configuration, temporary model failure, temporary retrieval failure, insufficient evidence, safety refusal, unauthorized citation, transient database error, constraint error, SQL error, and invariant/programming error.
- [ ] Use an explicit contract:

```python
class EmailDraftFailure(RuntimeError):
    def __init__(self, *, code: str, error_class: ErrorClass, retry_after_seconds: int | None = None): ...

@dataclass(frozen=True, slots=True)
class PreparedEmailDraft:
    body: str
    citations: tuple[EmailCitation, ...]
    provenance: EmailDraftProvenance
```

- [ ] Run `cd backend && pytest tests/unit/email/test_draft_failures.py tests/integration/email/test_draft_generation.py -q`; verify RED.
- [ ] Remove `_fail`, replacement-job creation, state mutation, and broad exception swallowing from the service. Map only known provider/retrieval/database conditions; unknown programming/SQL errors remain terminal and visible.
- [ ] Run the focused tests; verify GREEN and no draft retry job is created by the service.

## Task 10: Finalize Draft Success or Failure in One Worker Transaction

**Files:**
- Modify: `backend/app/modules/email/tasks.py`
- Modify: `backend/app/modules/jobs/service.py`
- Create: `backend/tests/integration/email/test_draft_job_reliability.py`

- [ ] Add tests for atomic success, success-finalization rollback, failure-finalization rollback, invalid external-call transaction requiring rollback before a new transaction, retry on the same job, exhausted cycle, lost lease, and canonical mismatch.
- [ ] Instrument the fake model to assert no database write lock is held during retrieval/model calls.
- [ ] Run `cd backend && pytest tests/integration/email/test_draft_job_reliability.py -q`; verify RED.
- [ ] Refactor `_consume_email_job` so draft preparation happens outside locks and finalization begins with a fresh transaction. Lock `JobIntent` then `EmailWorkItem`; recheck lease version, expiry, organization, business state, current draft, and `active_draft_job_id`.
- [ ] On success, persist draft version/body/citations/provenance, transition to `AWAITING_REVIEW`, add ready Outbox/audit, clear canonical pointer, and mark the job `SUCCEEDED` before one commit.
- [ ] On structured failure, rollback any failed transaction first, then in a fresh transaction update work item, original job retry/terminal state, and audit together. Retry only if the current cycle has remaining attempts.
- [ ] Restrict automatic database retries to known transient SQLSTATE/connection cases; never retry constraints, invalid SQL, data, or programming errors generically.
- [ ] Run reliability tests and existing job lease tests; verify GREEN.

## Task 11: Add Finite Manual Retry Cycles and Safe Duplicate Delivery

**Files:**
- Modify: `backend/app/modules/operations/service.py`
- Modify: `backend/app/modules/operations/router.py`
- Modify: `backend/app/modules/email/review.py`
- Modify: `backend/app/modules/email/tasks.py`
- Modify: `backend/tests/integration/operations/test_safe_manual_retry.py`
- Modify: `backend/tests/integration/email/test_draft_regeneration.py`

- [ ] Add tests where exhausted job has cumulative attempts 7; authorized retry starts cycle 2 at attempts 7 with limit 3; claims 8, 9, and 10 are the only permitted executions. Assert automatic code cannot increment the cycle.
- [ ] Add duplicate-delivery tests: already `SUCCEEDED` is a no-op; already terminal is unchanged; only a nonterminal noncanonical job becomes `FAILED/NON_RETRYABLE/EMAIL_DRAFT_SUPERSEDED`; no stale job changes the work item.
- [ ] Add Regenerate race test where old worker finishes after new canonical job creation and cannot publish over it.
- [ ] Run the two focused files; verify RED.
- [ ] Extend manual retry request with nonempty reason and expected job/work-item versions. In one transaction increment cycle, set start to cumulative attempts, set finite configured limit, return same job to `PENDING`, add Outbox, and audit operator/reason/cycle/limit.
- [ ] Make Regenerate create the new job, initial cycle metadata, state/audit/Outbox, and canonical pointer atomically. Fence late workers by canonical ID and lease.
- [ ] Preserve succeeded/terminal duplicate jobs exactly; never relabel a succeeded job as superseded.
- [ ] Add migration/runtime tests proving clearly stale nonterminal jobs may become `EMAIL_DRAFT_SUPERSEDED` while all task rows, state history, audits, Outbox events, and queue contents are retained. Assert no test helper deletes or clears historical records.
- [ ] Run focused tests and existing approval/delivery suites; verify GREEN.

## Task 12: Add Application Capability Grants Without Weakening Existing Checks

**Files:**
- Create: `backend/app/modules/authorization/application.py`
- Modify: `backend/app/modules/authorization/types.py`
- Modify: `backend/app/modules/authorization/policy.py`
- Create: `backend/app/modules/operations/application_authorization.py`
- Modify: `backend/app/modules/operations/schemas.py`
- Modify: `backend/app/modules/operations/router.py`
- Modify: `backend/app/modules/email/router.py`
- Modify: `backend/app/modules/support/router.py`
- Modify: `backend/app/modules/rag/router.py`
- Create: `backend/tests/integration/operations/test_application_access.py`
- Modify: `backend/tests/integration/email/test_review_authorization.py`
- Modify: `backend/tests/integration/authorization/test_resource_isolation.py`

- [ ] Add unit tests registering `ResourceType="application"`, `Action="application.access"`, and an ADMIN/REVIEWER-only active-resource role/state matrix that still requires a same-organization grant. Assert MEMBER denial.
- [ ] Add tests for UUIDv5 resource IDs derived from a fixed namespace plus `organization_id:capability`; assert Email/Support and organizations cannot collide and client-supplied IDs are recomputed/validated server-side.
- [ ] Add tests for deterministic per-org Email/Support application resources, active role/status, exact knowledge scope, cross-org denial, MEMBER denial, disabled users, revoked grants, and old sessions.
- [ ] Add `/api/v1/staff/knowledge/search` matrix tests for Email-only, Support-only, both, neither, missing `knowledge.review`, and cross-organization knowledge. Assert capability semantics are OR, knowledge scope remains mandatory, and client app name/Referer/page URL cannot influence the result.
- [ ] Add operation tests proving app access alone cannot approve/send email, claim/reply to support, review unassigned knowledge, or manage connectors.
- [ ] Add concurrent admin update and idempotency-key/content-conflict tests; assert mutation and audit roll back together and the operator is revalidated as active ADMIN inside the transaction.
- [ ] Run the focused authorization tests; verify RED.
- [ ] Register the action/resource literals and policy entries, then implement `application_access_resource_id(org_id, capability)` with UUIDv5 and a server-side resource resolver that validates organization/capability. Implement `require_application_access(capability)` as an additional dependency, not a replacement.
- [ ] Implement list/replace APIs returning grant presence, role/status eligibility, reviewable knowledge flag, exact knowledge-base scope, and effective allowed result. Require expected grant/user version and existing idempotency hash behavior.
- [ ] Apply the dependency to Email and Support entry APIs while leaving all existing `knowledge.review`, approval, delivery, takeover, reply, and connector checks in place. For shared knowledge search, require ADMIN/REVIEWER plus `(Email access OR Support access)` plus `knowledge.review` on the resolved knowledge base; ignore app names, Referer, and URLs from the client.
- [ ] Run authorization, email review, support, and resource-isolation suites; verify GREEN.

## Task 13: Build Server-Side Route Guards and the Public Homepage

**Files:**
- Create: `frontend/lib/application-access.ts`
- Create: `frontend/app/staff/email/layout.tsx`
- Create: `frontend/app/staff/support/layout.tsx`
- Create: `frontend/app/staff/admin/layout.tsx`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/globals.css`
- Modify: `frontend/tests/home.test.tsx`
- Create: `frontend/tests/route-access.test.tsx`
- Create: `frontend/e2e/home-access.spec.ts`

- [ ] Add component tests asserting exactly the six required destinations, labels, descriptions, public/protected badges, relative application URLs, configured liveness URL, and absence of `/chat/{publicKey}` and `/staff/email/{id}` menu links.
- [ ] Add route-guard tests for anonymous redirect/auth result, Email-only staff, Support-only staff, unauthorized staff denial, disabled staff denial, and ADMIN access to admin pages without Email/Support grants.
- [ ] Add Playwright assertions for desktop/mobile layout, keyboard tab order, visible focus, landmark headings, and reduced-motion compatibility.
- [ ] Run `cd frontend && npm test -- --run tests/home.test.tsx tests/route-access.test.tsx`; verify RED.
- [ ] Implement server-side `requireApplicationAccess` against the effective-access backend endpoint and add nested layouts. Never rely only on hidden navigation.
- [ ] Build the homepage with semantic HTML and existing CSS only; do not add a UI dependency. Read `NEXT_PUBLIC_LIVENESS_URL` with local default `http://localhost/health/live`; keep readiness relative.
- [ ] Run Vitest, `npm run typecheck`, and the focused Playwright spec; verify GREEN.

## Task 14: Extend Authorization Management and Gmail Push Operations UI

**Files:**
- Create: `frontend/components/admin/ApplicationAccessManagement.tsx`
- Create: `frontend/components/admin/GmailPushManagement.tsx`
- Modify: `frontend/app/staff/admin/authorization/page.tsx`
- Modify: `frontend/lib/staff-api.ts`
- Create: `frontend/tests/application-access-management.test.tsx`
- Create: `frontend/tests/gmail-push-management.test.tsx`

- [ ] Add tests for four separate sections: Application Access, Knowledge Base Assignments, Google Connector Authorization, and Gmail Push Setup.
- [ ] Assert effective access displays grant, role/status, reviewable knowledge, and exact scope separately; disabled users never show effective Allowed.
- [ ] Assert push status displays `auto_sync_enabled` separately from watch/sync status, hides secrets, supports Start/Stop/Renew/Resume/Manual sync/Continue, and requires contextual confirmation for Accept limited recovery.
- [ ] Assert confirmation includes window, count, truncation, possible missed range, impact, recovery batch, and expected version.
- [ ] Run `cd frontend && npm test -- --run tests/application-access-management.test.tsx tests/gmail-push-management.test.tsx`; verify RED.
- [ ] Add typed staff API methods with idempotency keys and optimistic versions. Reuse existing knowledge and connector components rather than duplicating their data stores.
- [ ] Implement accessible controls, pending/error states, conflict refresh, and explicit dangerous-action dialog. Keep OAuth, Pub/Sub IAM, and application access language separate.
- [ ] Run focused tests, all frontend tests, lint, and typecheck; verify GREEN.

## Task 15: Document Deployment, IAM, Recovery, and Safe Backlog Inspection

**Files:**
- Create: `docs/runbooks/gmail-push.md`
- Modify: `docs/runbooks/email-triage.md`
- Modify: `docs/runbooks/staff-auth.md`
- Modify: `README.md`
- Modify: `.env.example` if present
- Modify: `compose.yaml` only if environment wiring is required

- [ ] Add a documentation test or simple repository assertion that every backend setting and `NEXT_PUBLIC_LIVENESS_URL` appears in the runbook and README.
- [ ] Document `GMAIL_PUSH_ENABLED=false`, missing-config warnings, no one-minute fallback, build-time/rebuild behavior of `NEXT_PUBLIC_LIVENESS_URL`, daily renewal, six-hour reconciliation, 14-day receipts, and bounded recovery.
- [ ] Document exact Google principals separately:
  - Gmail publisher gets topic publisher;
  - Pub/Sub service agent gets token-creator permission where required;
  - push service account identifies the caller and gets Invoker only on hosting platforms that require it;
  - subscription operator gets `actAs` where required.
- [ ] Document public HTTPS/tunnel testing without weakening OIDC, start/stop/renew behavior, operation-generation recovery, cursor and page-token troubleshooting, truncated recovery acceptance, and administrator remediation.
- [ ] Add read-only SQL examples to inspect historical draft chains. Explicitly state that the migration does not purge jobs, audits, Outbox events, or Redis queues.
- [ ] State separately that this branch writes and tests migration/remediation code, but running it against a real historical database requires an explicit operator-approved deployment action and is not performed during development.
- [ ] Run link/config-name checks and `git diff --check`; fix only documentation errors introduced by this branch.

## Task 16: Full Regression and Delivery Evidence

**Files:**
- Modify only if a failing test exposes a defect in files already in scope.

- [ ] Run Ruff/format checks on changed Python files using the repository's configured commands.
- [ ] Run backend unit tests for Gmail gateway/push/config/draft errors.
- [ ] Run backend integration suites for migrations, Gmail push/watch/sync/recovery, history Outbox publish success/failure/duplicate/crash, empty-cursor initialization/recovery, draft reliability/regeneration/manual retry/stale preservation, authorization migration/shared-search matrix, jobs/Outbox, review, delivery, Support, Drive, and RAG.
- [ ] Run frontend Vitest, lint, typecheck, build, and focused Playwright homepage/access tests.
- [ ] Run `git diff --check`, inspect `git status --short --branch`, and verify the primary worktree's original modified and untracked files remain unchanged.
- [ ] Capture homepage desktop and mobile previews without exposing real data.
- [ ] Produce the delivery report with branch, confirmed root causes, changed files, migrations, push/cursor/lock flow, retained and removed schedules, draft retry-cycle behavior, access matrix, configuration, IAM/operator steps, exact test results, preview paths, known limitations, and deployment verification.
- [ ] Do not commit, push, merge, purge queues, contact real Gmail, create cloud resources, or send email.

## Execution Order and Checkpoints

Tasks 1-3 establish contracts and schema. Tasks 4-8 deliver Gmail push safely. Tasks 9-11 fix drafting independently of Gmail. Task 12 adds backend authorization before Tasks 13-14 expose it in the frontend. Task 15 documents the deployed contract, and Task 16 is the evidence gate.

After Tasks 2, 8, 11, and 14, stop for a review checkpoint and inspect the diff for scope, lock-order consistency, transaction boundaries, and preservation of unrelated files before proceeding.

## Follow-up Task After Current Scope: Initial Administrator Bootstrap CLI

This task is deliberately scheduled only after Tasks 1-16. Its example command is a proposed interface and is not executable until the task is implemented and tested.

- Reuse the existing invitation-gated Google OIDC flow; keep local passwords out of scope.
- Add a private one-shot CLI that creates one organization plus one `INVITED/ADMIN` user in the same transaction.
- Identify an existing organization by an explicit immutable organization UUID, never by a possibly duplicate display name. Creating a new organization requires an explicit new-organization mode.
- On repetition, return the existing matching bootstrap result without creating another organization/user, resetting identity, changing role, or promoting a non-admin user.
- Reject an existing email bound to a different organization, role, disabled state, or stable OIDC subject instead of repairing it implicitly.
- Preserve the current callback behavior: a verified Google email must exactly match the invitation; first login binds the stable `sub` and activates the user; mismatched, unverified, subject-conflicting, and disabled identities are denied.
- Do not expose an HTTP bootstrap endpoint and do not run bootstrap automatically at service startup.
- Add tests for successful first login after bootstrap, repeated initialization, email mismatch, unverified email, stable-subject mismatch, and disabled user.
- Document the eventual Docker Compose invocation only after the CLI exists; until then, no example command may be presented as executable.

## Review Resolution Checklist

- [x] **P1 Outbox:** Task 5 implements and tests the committed-event dispatcher chain, `published_at` ordering, retry, duplicate/crash window, Celery registration, and coordinated legacy fallback.
- [x] **P1 empty cursor:** Tasks 7-8 prevent `history.list(None)`, persist `H0` and recovery pagination, coalesce notifications, resume crashes, and handle truncation/explicit skip policy.
- [x] **P1 authorization:** Tasks 2 and 12 implement the effective legacy backfill predicate, type/action/policy registration, UUIDv5 resource resolution, and shared-search OR capability plus knowledge-review matrix.
- [x] **P2 Gmail parameters:** Task 3 freezes the exact watch body, history query, singular request event, and plural response field.
- [x] **P2 historical jobs:** Tasks 2, 11, and 15 preserve records/queues, terminate only clear stale nonterminal jobs, leave successful/terminal jobs unchanged, route ambiguity to humans, and prohibit unapproved real-database execution.
