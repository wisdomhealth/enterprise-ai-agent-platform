# Gmail Push, Draft Reliability, Homepage, and Access Control Design

## Goal

Replace one-minute Gmail polling with authenticated, durable Gmail push ingestion; stop repeated draft-job chains; add a public SaaS-style homepage; and add application capability management without weakening the platform's existing organization, resource, operation, approval, or delivery authorization.

This change reuses the existing `JobIntent`, Outbox, lease fencing, email state machine, `ResourceGrant`, idempotency, audit, Celery, Gmail OAuth, and staff-session mechanisms. It does not purge historical jobs, queues, audit data, or unrelated records, and it does not automatically send email.

## Confirmed Root Causes

### Repeated Gmail history jobs

`gmail-history-poll` runs every 60 seconds. `_enqueue_gmail_history_jobs` derives a base key from the unchanged cursor and page token, but after the matching job finishes it appends `:after:{job_id}:{version}`. Therefore every beat interval can create another `email.gmail_history` intent even when Gmail history has not changed.

### Repeated draft jobs marked successful

`EmailDraftingService.generate` catches drafting failures, moves the work item to `DRAFT_RETRY_WAIT`, increments its version, and enqueues a new version-keyed `email.draft` intent. It then returns normally. The outer worker interprets the return as success and marks the original job `SUCCEEDED`, producing the observed `v19`, `v21`, `v23`, and later chain.

## Architecture

The implementation has four coordinated workstreams:

1. Gmail `users.watch` and Pub/Sub push become the primary ingestion trigger.
2. Draft execution uses one canonical `JobIntent` per automatic drafting lifecycle and bounded retry cycles.
3. `ResourceGrant` gains coarse application-capability resources while existing fine-grained authorization remains mandatory.
4. The public homepage and authorization page expose the required routes and administration controls.

All external Gmail, Pub/Sub, broker, retrieval, and model calls run outside database write locks. Short database transactions validate generations and leases, lock records in one order, and atomically persist state changes, jobs, outbox events, and audit records. The existing Outbox dispatcher pattern is the authoritative broker-publication path for Gmail history jobs; no parallel publisher infrastructure is introduced.

## Data Model

### Email sync state

`EmailSyncState.history_id` is renamed to `processed_history_id` by an Alembic migration that preserves every existing value and updates all model, query, schema, serialization, and test references.

`processed_history_id` means that the corresponding Gmail changes have been persisted locally and all required downstream task intents and outbox records have been durably committed. It does not mean classification, drafting, review, or delivery succeeded. Model failures therefore never hold the ingestion cursor back.

When `processed_history_id` is `NULL`, the mailbox is not passed to `history.list`. It enters the same persisted bounded initialization/recovery workflow used for an expired cursor. Before scanning, the worker obtains and persists baseline `H0`, recovery batch identity, scan window, page position, processed count, and batch state. It then performs the bounded inbox scan, follows `history.list` from `H0`, and establishes the first processed cursor only after all pages and downstream intents are durable. A crash resumes the persisted batch rather than starting an untracked bootstrap. Notifications received meanwhile update pending high-water but do not create a competing normal sync generation.

`EmailSyncState` is extended with:

- `mailbox_address`: Gmail `getProfile` email address after trimming and case normalization. Dots and plus suffixes are never rewritten.
- `pending_history_id`: highest validated notification history ID not yet covered by a completed synchronization.
- `sync_start_history_id`: immutable start cursor for the current synchronization generation.
- `pending_page_token`: current page token for that generation.
- `page_token_generation`: generation owning the page token.
- `active_sync_job_id`: canonical active Gmail history job.
- `sync_generation`: monotonic synchronization generation.
- `sync_status`: `IDLE`, `RUNNING`, `PAUSED_ERROR`, `RECOVERY_REQUIRED`, `RECOVERY_RUNNING`, or `RECOVERY_TRUNCATED`.
- `auto_sync_enabled`: persistent administrator intent. Only an authorized Start/Resume enables it and Stop disables it.
- `watch_status`: `STOPPED`, `STARTING`, `ACTIVE`, `STOPPING`, or `ERROR`; this describes Gmail watch lifecycle, not whether notification work is allowed.
- `watch_operation_generation`: monotonic Start/Stop/Renew operation fence.
- `watch_topic_name`, `watch_expiration_at`, `watch_history_id`, `last_watch_success_at`, and sanitized watch error fields.
- `last_notification_at`, `last_success_at`, recovery batch metadata, truncation metadata, and optimistic `version`.

Persisted recovery metadata includes `recovery_batch_id`, `recovery_baseline_history_id`, `recovery_window_started_at`, `recovery_page_token`, `recovery_processed_count`, `recovery_has_more`, and `recovery_started_at`. `page_token_generation` continues to bind normal history pagination; recovery page position is bound to its recovery batch ID.

`auto_sync_enabled`, not `watch_status`, captures administrator intent:

- An authorized Stop sets it to `false` before calling Gmail. Delayed notifications and scheduled reconciliation may still be authenticated and diagnosed, but they cannot create or reactivate automatic synchronization work.
- A renewal failure may set `watch_status=ERROR` while the previously registered watch remains unexpired. If `auto_sync_enabled=true`, valid notifications continue to update pending work and may start synchronization.
- Manual synchronization is independent of `auto_sync_enabled` and remains available to authorized administrators.
- `STARTING` notifications are accepted after the trusted mailbox mapping and operation generation have been committed; the webhook does not wait for `ACTIVE`.

The product rule is one mailbox per organization binding: `mailbox_address` is globally unique among Gmail sync states. The address comes only from the verified Gmail profile attached to an authorized connector. Request-body mailbox text never establishes tenant ownership.

### Pub/Sub receipts

`gmail_push_receipts` stores organization, connector, subscription name, Pub/Sub message ID, notification history ID, publication time, receipt time, and expiry. Its unique key is `(subscription_name, pubsub_message_id)` because Pub/Sub message IDs are not assumed globally unique across topics or subscriptions.

Receipts are retained for 14 days by default and deleted in bounded batches. Identity, subscription, and mailbox validation precede deduplication. If a receipt was already cleaned, an old redelivery still cannot create empty work because numeric `notification_history_id <= processed_history_id` exits without setting pending work; Gmail message uniqueness and business states prevent duplicate downstream work.

### Draft canonical job and retry cycles

`EmailWorkItem.active_draft_job_id` points to the only draft job allowed to publish or fail the current automatic or explicit regeneration attempt.

Draft `JobIntent` records persist retry-cycle metadata:

- cumulative `attempts` is never reset;
- `attempt_cycle` identifies the current authorized retry cycle;
- `cycle_start_attempts` records cumulative attempts at cycle start;
- `cycle_attempt_limit` records the number of attempts allowed in that cycle.

`EMAIL_DRAFT_MAX_ATTEMPTS` includes the first execution in each cycle. Automatic scheduling may retry only while `attempts - cycle_start_attempts < cycle_attempt_limit`; it cannot create a new cycle. An authorized manual retry increments `attempt_cycle`, sets `cycle_start_attempts=attempts`, sets the new finite limit, moves the same failed job to `PENDING`, and atomically records operator, reason, cycle, and allowed attempts in audit data. Cumulative attempts remain intact.

Explicit reviewer Regenerate is distinct: it creates a new draft job and atomically replaces `active_draft_job_id`. A late old worker cannot publish because finalization rechecks the canonical job under lock.

### Application grants

Application access reuses `ResourceGrant`:

- `resource_type="application"`
- `action="application.access"`
- deterministic per-organization resources for `email` and `support`

No second knowledge-assignment store is added. Existing `knowledge.review` grants and APIs remain authoritative for knowledge-base scope.

## Gmail Watch Lifecycle

### Gmail request contracts

Watch registration sends the following Gmail `users.watch` request. `messageAdded` is a `users.history.list` event type; the watch request does not use the response field name `messagesAdded`.

```http
POST https://gmail.googleapis.com/gmail/v1/users/me/watch
Content-Type: application/json

{
  "topicName": "projects/<project-id>/topics/<topic-name>",
  "labelIds": ["INBOX"],
  "labelFilterBehavior": "include"
}
```

Incremental synchronization sends:

```http
GET https://gmail.googleapis.com/gmail/v1/users/me/history
    ?startHistoryId=<fixed-generation-start-id>
    &historyTypes=messageAdded
    &labelId=INBOX
    [&pageToken=<provider-page-token>]
```

The watch label filter limits which changes may trigger notifications. It does not replace `history.list` filtering or the application's own message/business-scope filtering. The history response contains a `messagesAdded` collection, but the request parameter remains singular `messageAdded`.

### Start

Start requires an active same-organization administrator, a valid Gmail connector, push configuration, and an authenticated OAuth connection.

1. Call Gmail `getProfile` outside a write lock and verify the canonical mailbox.
2. In a short transaction, lock `EmailSyncState`, verify connector ownership, persist the trusted mailbox mapping, set `auto_sync_enabled=true`, set `watch_status=STARTING`, increment `watch_operation_generation`, and commit the operation identifier.
3. Call `users.watch` outside the transaction.
4. In a second transaction, lock the state and apply the response only if the operation generation and enabled intent still match. Persist the returned watch anchor and expiration without advancing either processed or pending cursors.
5. If a newer Stop or Start changed the generation, discard the delayed completion and audit it as superseded.

Gmail may publish an immediate notification between steps 3 and 4. Because the trusted mapping and enabled intent were committed in step 2, the webhook can durably store and enqueue that notification while status remains `STARTING`.

If `users.watch` succeeds but the local completion transaction fails, the state remains `STARTING` with a known operation generation. Scheduled watch reconciliation calls `getProfile` and safely re-registers the watch; re-registration is idempotent at the processing layer because watch anchors never advance processed cursors.

### Stop

1. In a short transaction, lock the sync state, set `auto_sync_enabled=false`, set `watch_status=STOPPING`, increment `watch_operation_generation`, and commit.
2. Call `users.stop` without a database lock.
3. Apply `STOPPED` only if the same operation generation remains current. If the remote call fails, record `ERROR` while keeping `auto_sync_enabled=false`.

The first transaction enforces administrator intent even if the network call is slow or fails. Delayed notifications cannot enqueue automatic work, and low-frequency reconciliation cannot bypass Stop. A delayed Start or Renew completion cannot overwrite the newer Stop because its generation is stale.

### Renew

Renew is permitted only when automatic synchronization is enabled. It reserves a new operation generation, calls `users.watch` without a write lock, and applies the response with a generation compare-and-set. It never changes processed or pending cursors.

If renewal fails while the previous expiration is still in the future, status records the error but valid push notifications remain processable. If the watch is expired, watch reconciliation registers a replacement; sync recovery state is changed only if Gmail history processing itself requires recovery.

### Manual synchronization

An authorized administrator may request a manual synchronization even while automatic synchronization is stopped. The request uses the same mailbox lock, cursor rules, generation fencing, and message deduplication, but is marked `trigger=MANUAL` and does not enable or start a watch.

## Authenticated Pub/Sub Webhook

The endpoint is `POST /api/v1/webhooks/gmail/pubsub`.

Before reading tenant state, it validates:

- body size and Pub/Sub envelope structure;
- JWT signature against Google keys;
- issuer, expiration, audience, subject, `email`, and `email_verified`;
- exact expected push service-account email;
- exact configured subscription name;
- base64 JSON data containing mailbox email and numeric Gmail history ID.

After token and subscription validation, the normalized mailbox is resolved through the unique stored Gmail profile mapping to one active connector and organization. Organization and connector IDs from the request are ignored.

In one transaction, with `EmailSyncState` locked first:

1. Insert the receipt using `(subscription_name, pubsub_message_id)` conflict suppression.
2. If the validated receipt already exists, return success without new work.
3. Update diagnostic notification timestamps.
4. If `notification_history_id <= processed_history_id`, retain the receipt and return success without changing pending work.
5. If `auto_sync_enabled=false`, retain the receipt and audit suppression without setting pending work or creating a job.
6. Otherwise set `pending_history_id` to the numeric maximum.
7. If no valid active sync job exists and sync is not paused for a terminal error, increment `sync_generation`, create one `email.gmail_history` `JobIntent`, add its Outbox request, and set `active_sync_job_id`.
8. Commit, then acknowledge with a 2xx response.

Any persistence failure returns non-2xx so Pub/Sub redelivers. A notification is never marked handled before the pending high-water mark and required job/outbox state are durable.

### Outbox publication

The webhook writes `email.gmail_history.requested` with `aggregate_type="job"` and `aggregate_id=<JobIntent.id>` in the receipt transaction. After commit, returning 204 does not wait for broker publication or worker execution.

The existing Email Outbox dispatcher is extended with an event-to-task mapping:

- `email.gmail_history.requested` -> `app.modules.email.tasks.email_job(<job_id>)`;
- existing delivery event mappings remain unchanged.

The dispatcher loads committed unpublished events, validates type, aggregate, job kind, and payload identity, publishes to Celery, increments `publish_attempts`, and sets `published_at` only after `delay` returns successfully. Broker failure leaves `published_at=NULL`; the scheduled pending-event sweep retries it. A crash after successful broker publication but before the database update may publish again. That at-least-once window is intentional and safe because the consumer claims the same `JobIntent` lease and all synchronization effects are generation/idempotency fenced.

The existing pending-`JobIntent` scan remains only a recovery safety net for legacy or malformed historical jobs that lack a usable Outbox event, and for lease-expired execution recovery. It is not the normal publisher for newly created Gmail history jobs. It must not publish a pending job while a valid unpublished `email.gmail_history.requested` event exists; this prevents two uncoordinated routine publishers. Scanning pending Outbox events may run every minute, but it is broker-delivery recovery, not periodic Gmail mailbox polling and does not create new Gmail pull jobs.

## Gmail Synchronization and Cursor Rules

History IDs are parsed as integers for comparison. They are monotonic but not consecutive and are never compared lexicographically.

Each synchronization generation fixes `sync_start_history_id=processed_history_id`. Every page token is tagged with that start cursor and generation. Gmail calls happen without locks. Each page is committed in a short transaction that locks the sync state, checks the generation and active job, persists newly eligible messages, downstream classification intents, and outbox records, and then records the next page token.

This normal generation path requires non-`NULL` `processed_history_id`. A `NULL` cursor is routed to persisted initialization/recovery before any history call. While that batch is running, incoming notification high-water is retained on the same sync state and no second normal job is created.

Only existing business-scope incoming inbox messages are ingested. Message-ID uniqueness remains scoped by organization, and Gmail fetch/filter rules prevent sent mail, platform-created drafts, and label-only changes from starting classification or drafting loops.

If a page token is invalid while the fixed start history cursor remains valid, the worker clears only that generation's page token and rereads from the same start cursor. Message uniqueness and idempotent task keys absorb repeats. Cursor expiry is a distinct Gmail 404/history error and enters controlled recovery.

After the final page, the worker uses the history ID returned by that complete Gmail response as the new `processed_history_id`; it never copies `pending_history_id` into the processed cursor. In the final transaction it clears the active job only if the job and generation still match, then:

- if pending is now covered, clear pending and move to `IDLE`;
- if a newer pending history exists, atomically create the next generation's job and Outbox record;
- if automatic synchronization was stopped during execution, retain diagnostics and do not create a follow-up automatic job.

One mailbox has at most one effective synchronization executor. All webhook, worker claim/finalization, watch actions, reconciliation, manual recovery, and cleanup paths use the same lock order:

`EmailSyncState -> JobIntent rows ordered by ID -> EmailWorkItem rows ordered by ID -> Outbox/Receipt rows ordered by ID`.

A crashed worker retains `active_sync_job_id`; the existing expired-lease recovery reclaims the same job. On non-retryable failure or exhausted retries, the final transaction clears the active reference, preserves pending work, sets `sync_status=PAUSED_ERROR`, records the sanitized cause, and stops automatic job creation. Later notifications may raise pending high-water but cannot create endless replacement jobs. After an administrator fixes credentials or configuration, Resume atomically verifies the repair, replaces the failed reference with one new generation, and continues from the persisted processed cursor.

## Cursor Recovery and Reconciliation

Push is primary. The 60-second Gmail history beat entry and poll task are removed. Outbox dispatch, pending job dispatch, lease recovery, delivery reconciliation, retention, and unrelated schedules remain unchanged.

Configurable low-frequency tasks perform:

- daily watch renewal/reconciliation;
- missed-notification synchronization, default every six hours, only where `auto_sync_enabled=true`;
- receipt cleanup in bounded batches.

If push configuration is disabled or incomplete, the application reports an administrator-visible warning and does not silently restore one-minute polling.

An expired history cursor enters bounded recovery:

1. Obtain Gmail profile baseline `H0` before scanning.
2. Scan inbox mail from the previous seven days, at most 500 messages by default.
3. Persist eligible messages using existing message-ID deduplication.
4. Run incremental history from `H0` to capture messages arriving during the scan.
5. Commit the final valid history response as processed only after pagination and downstream intents are durable.

First enablement with a `NULL` cursor follows exactly these steps and is not a silent skip-to-current operation. The recovery batch stores `H0`, scan/recovery page token, processed count, limits, and status after each durable page so a new process resumes the same batch. The optional policy “only process mail arriving after enablement” is available only as an explicit administrator choice before initialization, requires confirmation and audit, and is never the default.

Notifications arriving during recovery still raise `pending_history_id`. If the scan reaches the cap with more results, status becomes `RECOVERY_TRUNCATED`; the UI reports window, processed count, truncation, and possible missed range. It never claims a complete recovery. An administrator may Continue with another bounded batch or explicitly Accept limited recovery. Acceptance requires a detailed confirmation, records administrator, time, choice, and recovery batch, and still completes the `H0` incremental catch-up before cursor finalization. A failed or expired `H0` remains in recovery state rather than jumping to the latest profile history ID.

## Draft Execution and Transactions

`EmailDraftingService` becomes a pure preparation component: it validates the principal, invokes retrieval/model work, validates citations, and returns either a prepared draft or a structured `EmailDraftFailure`. It does not mutate failure state, enqueue replacement jobs, commit, or swallow exceptions.

The worker owns transaction boundaries:

1. Claim the canonical `JobIntent` and record its lease version.
2. Read the work item without holding a write lock; reject already terminal or noncanonical work before external calls.
3. Perform retrieval/model work outside database locks.
4. Start a fresh final transaction. Lock the claimed `JobIntent` first and the `EmailWorkItem` second, matching the shared `JobIntent -> EmailWorkItem` order when no sync state participates; then revalidate lease, `active_draft_job_id`, organization, version, state, and current draft.
5. Atomically persist one of the success or failure outcomes.

Success transaction:

- persist the generated draft version and citation/provenance data;
- transition the item to `AWAITING_REVIEW`;
- write ready Outbox and audit events;
- clear `active_draft_job_id`;
- mark the same `JobIntent` `SUCCEEDED`;
- commit once.

This removes the window where a draft is committed but its job remains retryable.

Failure transaction:

- if the external-call transaction is invalid, roll it back before any query;
- lock and revalidate the lease and canonical job;
- store the structured error and state transition on the work item;
- update the original job to delayed `PENDING` or terminal `FAILED` according to the current retry cycle;
- write failure audit data;
- commit once.

No query or commit is attempted on a failed SQLAlchemy transaction. Only confirmed transient database conditions such as serialization failure, deadlock, or lost connection are retryable. Constraint violations, invalid SQL, data errors, and programming errors are terminal and visible.

Draft error reasons are distinct:

- temporary provider throttling with retry guidance: retryable;
- quota exhaustion, billing, invalid credentials, and configuration: non-retryable until authorized manual recovery;
- temporary model or retrieval dependency failure: retryable within the current cycle;
- insufficient supporting knowledge: `EMAIL_DRAFT_INSUFFICIENT_EVIDENCE`, human attention, no automatic retry;
- model safety refusal: `EMAIL_DRAFT_SAFETY_REFUSAL`, human attention, no automatic retry;
- unauthorized or invalid citations: security/non-retryable;
- confirmed transient database concurrency/connection error: retryable;
- constraint, SQL, and invariant errors: non-retryable.

### Historical draft-job migration and duplicate delivery

The migration and post-migration reconciliation use actual work-item state, `EmailStateHistory.job_id`, job payload association, current draft, and job state rather than parsing the largest `:vN` suffix:

- items with a completed current draft or `AWAITING_REVIEW` receive no active job and are never regenerated;
- terminal failed jobs remain terminal;
- when exactly one pending/running associated job is consistent with a drafting state, it becomes canonical;
- when canonical ownership cannot be determined, the item enters existing `FAILED_TERMINAL` with `EMAIL_DRAFT_CANONICAL_JOB_AMBIGUOUS` for explicit administrator action;
- stale nonterminal jobs are terminated with `EMAIL_DRAFT_SUPERSEDED` without changing the work item;
- already `SUCCEEDED` or already terminal duplicate deliveries are no-ops and are never rewritten as superseded.

Regenerate creates the new job, retry-cycle metadata, state transition, audit record, Outbox request, and canonical reference in one transaction. A late old task cannot overwrite the regenerated draft.

## Authorization

Application access is a coarse first gate, never a replacement for existing checks.

For every Email or Support API, effective authorization is:

`active account + allowed role + same organization + application.access + resource visibility/scope + operation-specific permission`.

Existing permissions for `knowledge.review`, draft approval, email send, conversation claim/takeover, customer reply, connector configuration, and administrator operations remain independently required. A MEMBER cannot gain review, approval, send, or takeover ability from an application grant.

Admin pages remain restricted by active ADMIN role and do not require Email or Support application grants, so an administrator can always repair access from the authorization page.

Existing active ADMIN/REVIEWER users receive application grants only where their existing legitimate grants already make the capability usable. Migration does not add knowledge-base grants, does not broaden resource scope, and adds no MEMBER review capability. New users default to denied until an administrator assigns access.

The exact backfill predicate is: the user belongs to the target organization, is active, has role ADMIN or REVIEWER, and under the pre-change authorization policy has effective `knowledge.review` access to at least one active knowledge base in that organization. Effective access is calculated through the existing role/action/state/grant policy, including any actual administrator implicit-access rule if present; it is not reduced to checking for one literal grant row. Matching legacy users receive both Email and Support application access solely to preserve their previously valid entry capability. MEMBER, disabled, cross-organization, and users without effective review access receive no application grant.

`authorization/types.py` registers `resource_type="application"` and action `application.access`. `authorization/policy.py` permits that action only to ADMIN and REVIEWER on active application resources, then requires the normal same-organization `ResourceGrant`. Deterministic application resource IDs are UUIDv5 values derived from a fixed application namespace plus `organization_id:capability`; resource resolution recomputes the ID server-side and verifies the embedded organization/capability context rather than trusting a client ID.

Authorization updates:

- lock and revalidate the operator as an active same-organization administrator;
- lock the target user and relevant grant;
- require optimistic version/precondition data so two administrators cannot silently overwrite each other;
- scope idempotency by organization, operator, operation, target resource, and key;
- reject reuse of the same key with different request content;
- commit grant changes and audit together;
- enforce every request server-side so revoked grants, disabled users, stale pages, and old sessions cannot bypass current authorization.

The effective-access response separately reports application grant, role/status eligibility, whether any reviewable knowledge base exists, and the exact accessible knowledge-base IDs. The UI never displays a blanket Allowed merely because the application grant exists.

The shared `POST /api/v1/staff/knowledge/search` boundary requires all of:

- a valid active staff session;
- an ADMIN or REVIEWER role permitted to review;
- Email application access **or** Support application access;
- `knowledge.review` permission on the resolved knowledge base.

Email and Support capability checks use OR, not AND. Either capability opens the shared search surface, but neither permits searching an unassigned or cross-organization knowledge base. The server chooses and resolves the knowledge base from trusted organization/resource state; authorization never depends on a client application name, `Referer`, or page URL. The administrator authorization page remains ADMIN-only and requires neither capability grant.

## Routes and Pages

### Public homepage

`/` remains public and uses the existing Next.js application and CSS. It contains a brand/header, concise hero, three feature cards, a short workflow, and all six required destinations:

1. `/staff/email` — Email Assistant
2. `/staff/support` — Support Console
3. `/staff/admin` — Admin Dashboard
4. `/staff/admin/authorization` — Authorization Management
5. configurable local default `http://localhost/health/live` — Liveness
6. `/health/ready` — Readiness

Normal application links are relative. Protected entries display access requirements. `/chat/{publicKey}` and `/staff/email/{id}` remain functional but are not added to the homepage menu. The page uses accessible contrast, semantic landmarks, keyboard focus, reduced-motion handling, and responsive layouts without invented metrics, testimonials, or customer data.

`NEXT_PUBLIC_LIVENESS_URL` defaults to `http://localhost/health/live`. Because it is a Next.js public variable embedded in the client build, changing it requires rebuilding and redeploying the frontend image. Production supplies its public liveness origin at build time.

### Route guards

`/staff/email` and its child routes require an active staff session plus effective Email application access before rendering. `/staff/support` similarly requires Support application access. Admin routes require active ADMIN role and do not depend on those grants. Anonymous users are directed to authentication; authenticated unauthorized users receive a clear access-denied page. Backend APIs independently repeat all required checks.

### Authorization management

`/staff/admin/authorization` contains four clearly separate sections:

1. Application Access matrix showing grant, role/status eligibility, reviewable knowledge availability, and effective resource scope.
2. Knowledge Base Assignments using existing `ResourceGrant` APIs.
3. Google Connector Authorization using existing OAuth and connector grant flows.
4. Gmail Push Setup showing sanitized watch status, explicit automatic-sync enabled state, expiration, last notification, last successful sync, processed/pending summary, recovery state, and errors.

Authorized actions include Start, Stop, Renew, Resume synchronization, Manual sync, Continue recovery, and Accept limited recovery. Dangerous recovery acceptance requires explicit contextual confirmation. OAuth authorization, Pub/Sub/IAM configuration, and application access are presented as separate controls.

### Health endpoints

Homepage and liveness/readiness remain public. Readiness exposes only minimal dependency availability and never connection strings, customer data, credentials, or stack traces. Local URLs follow the requested HTTP/HTTPS split; production origins are configuration driven.

## Configuration and Deployment

Backend settings:

- `GMAIL_PUSH_ENABLED=false` by default.
- `GMAIL_PUBSUB_TOPIC`
- `GMAIL_PUBSUB_SUBSCRIPTION`
- `GMAIL_PUSH_ENDPOINT_URL`
- `GMAIL_PUSH_OIDC_AUDIENCE`
- `GMAIL_PUSH_SERVICE_ACCOUNT_EMAIL`
- `GMAIL_PUSH_MAX_BODY_BYTES`
- `GMAIL_WATCH_RENEWAL_INTERVAL_SECONDS` (default one day)
- `GMAIL_WATCH_RENEW_BEFORE_SECONDS`
- `GMAIL_RECONCILIATION_INTERVAL_SECONDS` (default six hours)
- `GMAIL_PUSH_RECEIPT_RETENTION_DAYS` (default 14)
- `GMAIL_RECOVERY_LOOKBACK_DAYS` (default 7)
- `GMAIL_RECOVERY_MAX_MESSAGES` (default 500)
- `EMAIL_DRAFT_MAX_ATTEMPTS` (default 3, including first execution per cycle)
- `EMAIL_DRAFT_RETRY_BASE_SECONDS`
- `EMAIL_DRAFT_RETRY_MAX_SECONDS`

Frontend build setting:

- `NEXT_PUBLIC_LIVENESS_URL=http://localhost/health/live`

When push is enabled but required settings are missing, configuration validation or readiness/admin status reports the exact missing nonsecret fields. When push is disabled, no Gmail polling fallback is scheduled.

Google Cloud documentation distinguishes these principals:

- Gmail publisher `gmail-api-push@system.gserviceaccount.com`: `roles/pubsub.publisher` on the topic.
- Pub/Sub service agent: permission to mint the authenticated push token for the selected push service account, typically `roles/iam.serviceAccountTokenCreator` on that service account where required.
- Push service account: identity placed in the OIDC token; it receives service invocation permission only when the actual hosting platform, such as Cloud Run, requires it.
- Operator configuring the subscription: `iam.serviceAccounts.actAs` where Google Cloud requires attaching the push service account.

Invoker roles are not prescribed for self-hosted ingress or platforms that do not use that IAM gate. Google cannot push directly to localhost; local testing uses a narrowly exposed HTTPS tunnel or test environment while preserving OIDC validation and limiting the exposed path.

## Logging and Auditing

Structured logs and audit events include organization, connector, work item, job, Pub/Sub message ID, stage, error code, retryability, and next attempt where relevant. IDs remain structured fields rather than high-cardinality Loki labels. Logs exclude OAuth tokens, service-account keys, full email bodies, and unnecessary personal data.

Audit covers watch Start/Stop/Renew/Resume, manual sync, recovery decisions, notification suppression, synchronization outcomes, application grant changes, manual draft retry cycles, regeneration, and draft failures.

## Test Strategy

Tests use isolated databases and mocked Gmail, Pub/Sub JWT, retrieval, and model clients. They do not call real Gmail, create cloud resources, send email, purge queues, or use paid models.

### Gmail push and recovery

- no default 60-second Gmail poll schedule;
- valid push creates durable incremental work;
- invalid signature, issuer, expiry, audience, service account, subscription, payload, size, or mailbox mapping is rejected;
- identity and mailbox validation occurs before receipt deduplication;
- receipt, pending high-water, job, and outbox commit atomically before acknowledgment;
- history Outbox publication succeeds only before `published_at`, failed publication stays pending, repeated dispatcher delivery is safe, and broker success followed by a pre-update crash is safely redelivered;
- database failure returns non-2xx;
- duplicate, old, out-of-order, and cleaned-receipt redeliveries create no duplicate or empty work;
- notification during active synchronization produces exactly one safe follow-up generation;
- only one effective executor exists per mailbox;
- terminal sync failure pauses without permanent active-job blockage or endless replacement jobs;
- page-token invalidity rereads the fixed start cursor without loss or duplicate work;
- cursor expiry performs `H0` scan then incremental catch-up;
- first enablement with an empty cursor persists initialization `H0`, never calls `history.list` with `NULL`, coalesces concurrent notifications, resumes after a crash, and reports bounded-scan truncation;
- messages arriving during recovery are captured;
- truncation is not reported as complete;
- limited recovery acceptance is confirmed, audited, and still performs `H0` catch-up;
- classification/model failure does not block processed cursor advancement;
- sent, draft, and label-only changes do not loop;
- notification arriving before Start completion is retained and processed;
- concurrent Stop defeats delayed Start/Renew completion;
- renewal failure with an unexpired prior watch still permits valid notifications;
- Stop prevents both delayed notification enqueue and scheduled reconciliation enqueue while allowing authorized manual sync.

### Drafting

- transient failures retry the same job with cumulative attempts and bounded backoff;
- failure is never marked `SUCCEEDED`;
- failure transaction rollback leaves neither partial work-item nor job/audit state;
- success persists draft, review state, canonical clearing, audit/outbox, and `SUCCEEDED` atomically;
- artificial finalization failure rolls back all success effects;
- manual retry starts one finite cycle without resetting cumulative attempts;
- automatic scheduling cannot start a new cycle;
- historical migration selects canonical jobs from actual relationships and sends ambiguous items to human attention;
- duplicate delivery of `SUCCEEDED` is a no-op;
- only applicable nonterminal stale jobs become superseded;
- Regenerate atomically changes the canonical job and defeats late old completion;
- quota/billing, safety refusal, insufficient evidence, transient provider, and transient versus invalid database failures receive distinct classifications.

### Authorization and UI

- existing-user grant migration preserves but never expands knowledge access;
- legacy ADMIN implicit review access, explicit REVIEWER access, inactive users, MEMBER users, and cross-organization grants satisfy the exact migration predicate;
- MEMBER cannot gain review ability from an application grant;
- disabled account, revoked grant, stale session, and stale frontend cache cannot access APIs;
- app access does not replace send, approve, claim, reply, or configuration authorization;
- shared knowledge search permits Email-only, Support-only, or both capability grants only with `knowledge.review`, and rejects neither, missing knowledge scope, and cross-organization cases;
- concurrent administrator changes and idempotency-content conflicts return safe conflicts;
- grant mutation and audit roll back together;
- all six homepage links, labels, descriptions, and access badges are present;
- desktop, mobile, keyboard, focus, and reduced-motion behavior are covered;
- anonymous, staff, and administrator route behavior matches the matrix;
- readiness output remains minimal;
- Gmail gateway tests assert the exact `users.watch` body and `users.history.list` parameters, including singular `messageAdded` request and plural `messagesAdded` response handling;
- stale nonterminal draft jobs can be terminated without deleting history, while successful/terminal jobs and queues remain unchanged;
- existing Drive, RAG, approval, delivery, Outbox, lease, and retention workflows remain green.

## Operational Boundaries

This feature does not create or modify real Google Cloud resources, delete historical task or audit records, or clear queues. Migration and runtime consumers may safely terminate stale nonterminal jobs that can be identified unambiguously and record `EMAIL_DRAFT_SUPERSEDED`; already successful or already terminal jobs are not rewritten as another failure state. Work items without a reliably identifiable canonical job enter explicit human-attention state.

Writing and testing migration/remediation code is part of this branch. Executing that migration or remediation against a real historical database is a separate operator action and is not authorized by this work. No commit, push, or merge is part of implementation unless explicitly requested.

Until the authenticated push receiver, transactional Outbox write, broker dispatcher, and leased Worker consumer all pass end-to-end tests, this branch does not provide complete Gmail ingestion and must not replace the running version.

Initial-administrator bootstrap remains a follow-up immediately after this scope. It will reuse the existing invitation and Google OIDC activation path, atomically create a new organization and `INVITED/ADMIN` user without a password, use explicit organization UUID/new-organization modes, remain idempotent without promotion or password/identity reset, and expose no public endpoint. The existing callback must continue to require verified exact-email matching, bind the stable Google `sub`, activate a valid invitation, and reject mismatched, conflicting, or disabled users. No bootstrap command is described as executable until that CLI and its first-login/idempotency/security tests exist.

## Review Resolution Matrix

- **P1 Outbox chain:** receipt, job, and Outbox share the webhook transaction; the existing Email dispatcher owns `email.gmail_history.requested`; `published_at` changes only after publish; pending-job scanning is explicitly recovery-only and coordinated with Outbox.
- **P1 NULL cursor:** first enablement persists `H0` and recovery progress, performs bounded scan plus `H0` incremental catch-up, coalesces concurrent notifications, survives crashes, and never calls `history.list(None)`.
- **P1 application authorization:** the legacy effective-policy predicate, application type/action registration, deterministic organization-bound resources, role matrix, and shared-search OR capability plus knowledge-review checks are explicit.
- **P2 Gmail parameters:** `users.watch` body and `users.history.list` query are listed separately, using request event `messageAdded` and response field `messagesAdded` correctly.
- **P2 historical jobs:** stale nonterminal termination, successful/terminal preservation, ambiguous canonical human handling, and the separation between code creation and real-database execution are uniform.
