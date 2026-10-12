# Gmail triage operations

Task 17 reads Gmail and creates review-only work items. It never sends email. Sending remains
disabled until a later reviewed delivery intent is implemented.

## Connector boundary

The Gmail OAuth client requests exactly `gmail.readonly` and `gmail.send`. Employee Google OIDC
tokens are never used by this connector. Refresh tokens are loaded only through the encrypted
connector service and are not written to logs, audit details, Outbox events, job payloads, or raw
email references.

`GOOGLE_GMAIL_CLIENT_ID` and `GOOGLE_GMAIL_CLIENT_SECRET` must come from the deployment secret
manager. The connector encryption settings described in `docs/runbooks/connectors.md` must also be
configured. Do not use a real customer mailbox in CI or local tests; the test suite uses fixed
gateway responses.

## Durable ingestion flow

Celery Beat schedules `app.modules.email.tasks.gmail_history_poll` every 60 seconds. The scheduler
creates a PostgreSQL `JobIntent` for each active Gmail connector and dispatches the durable job.
`dispatch_pending_email_jobs` sweeps pending, due-retry, and expired-lease email jobs every minute,
so Redis or worker restarts do not lose committed work.

Each Gmail page, its normalized work items, one durable `email.classify` intent per new work item,
Outbox events, and the corresponding history/page cursor are committed together. Classification is
not performed inside the Gmail history job. A failed page rolls back the entire page and leaves the
previous cursor resumable. The bootstrap history anchor is retained across all bootstrap pages so
messages arriving during the initial scan are collected by the next history poll.
`(organization_id, gmail_message_id)` prevents duplicate work items.

The stored raw reference is an opaque `gmail://` locator, never raw MIME content or an access token.
Sender, recipients, subject, plain-text body, thread ID, history ID, and received timestamp are
normalized before persistence.

## Classification and drafting

Structured OpenAI classification accepts only the exact category values `ACTION_REQUIRED`, `INFORMATIONAL`,
`SPAM`, `UNKNOWN`; priority values `HIGH`, `NORMAL`, `LOW`; and a required boolean
`reply_required`. Extra, missing, inconsistent, or unknown fields fail closed.

`ACTION_REQUIRED` and `UNKNOWN` enter `DRAFTING` and receive a durable draft job. `INFORMATIONAL`
and `SPAM` stay visible as ingested work without an automatic send path. Draft generation uses the
staff RAG audience and persists only organization- and knowledge-base-authorized citations plus
model, prompt, retrieval, latency, token, and estimated-cost provenance. Successful drafts stop at
`AWAITING_REVIEW`.

The classification worker calls the model without holding an `email_work_items` row lock, then
locks the row only while persisting the result. A failure moves an `INGESTED` item to
`DRAFT_RETRY_WAIT`, stores a safe error code, and updates the same `email.classify` `JobIntent`.
It never creates a successor classification job. Provider response bodies, exception text, API
keys, subjects, and message bodies are not written to job, audit, or structured log fields.

Transient failures use exponential backoff with jitter on the same job. The defaults are three
total attempts, a five-second base delay, and a 300-second delay cap. Configure them with
`EMAIL_CLASSIFICATION_MAX_ATTEMPTS`, `EMAIL_CLASSIFICATION_RETRY_BASE_SECONDS`, and
`EMAIL_CLASSIFICATION_RETRY_MAX_SECONDS`. OpenAI SDK retries remain disabled, so this durable job
policy is the only retry owner. A provider `Retry-After` value is honored but capped by the same
maximum delay.

The safe classification codes are:

- Non-retryable: `EMAIL_CLASSIFICATION_AUTHENTICATION_FAILED`,
  `EMAIL_CLASSIFICATION_MODEL_ACCESS_DENIED`, `EMAIL_CLASSIFICATION_INSUFFICIENT_QUOTA`,
  `EMAIL_CLASSIFICATION_INVALID_REQUEST`, `EMAIL_CLASSIFICATION_CONFIGURATION_INVALID`, and
  `EMAIL_CLASSIFICATION_UNCLASSIFIED_FAILURE`.
- Retryable: `EMAIL_CLASSIFICATION_RATE_LIMITED`, `EMAIL_CLASSIFICATION_TIMEOUT`,
  `EMAIL_CLASSIFICATION_CONNECTION_FAILED`, `EMAIL_CLASSIFICATION_SERVER_ERROR`, and
  `EMAIL_CLASSIFICATION_INVALID_RESPONSE`.
- Historical duplicate: `EMAIL_CLASSIFICATION_SUPERSEDED`; the duplicate terminates before a paid
  model call while the oldest non-succeeded intent remains authoritative.

After the attempt limit, retryable failures become `FAILED` with no `next_attempt_at`. Their
`error_class` remains `RETRYABLE` to describe the cause; automatic scheduling does not reactivate a
failed job.

## Reauthorization and recovery

A Gmail 401, 403, revoked grant, or invalid grant changes the connector to `REAUTH_REQUIRED`, stops
further automatic ingestion, records `GMAIL_REAUTH_REQUIRED`, and emits the safe
`connector.reauthorization_required` Outbox event. Reauthorize through the existing administrator
connector OAuth flow. Do not force a job to success or manually move the Gmail cursor.

For a stalled connector:

1. Confirm the connector is `ACTIVE`; if it is `REAUTH_REQUIRED`, complete administrator
   reauthorization first.
2. Inspect the latest `email.gmail_history`, `email.classify`, or `email.draft` job state and safe
   error code. Never inspect or copy decrypted tokens.
3. Confirm Beat and worker processes are running. The pending-job sweep will recover committed jobs
   and expired leases automatically.
4. Verify `email_sync_states.history_id` and `pending_page_token` only after the complete page and
   work-item transaction is present.
5. Run the focused recovery tests against an isolated PostgreSQL database before any manual repair.

## Classification backlog and manual recovery

Use read-only queries first. The following query identifies work items with multiple historical
classification intents without changing production data:

```sql
SELECT
    payload->>'organization_id' AS organization_id,
    payload->>'work_item_id' AS work_item_id,
    count(*) AS job_count,
    array_agg(id ORDER BY created_at, id) AS job_ids,
    array_agg(state ORDER BY created_at, id) AS states
FROM job_intents
WHERE kind = 'email.classify'
GROUP BY payload->>'organization_id', payload->>'work_item_id'
HAVING count(*) > 1
ORDER BY job_count DESC, work_item_id;
```

Use this read-only query to find terminal classification failures and their recoverable work-item
state:

```sql
SELECT
    j.id AS job_id,
    j.payload->>'organization_id' AS organization_id,
    j.payload->>'work_item_id' AS work_item_id,
    j.state,
    j.attempts,
    j.error_class,
    j.last_error_code,
    j.next_attempt_at,
    e.state AS work_item_state,
    e.category
FROM job_intents AS j
JOIN email_work_items AS e ON e.id::text = j.payload->>'work_item_id'
WHERE j.kind = 'email.classify'
  AND j.state = 'FAILED'
ORDER BY j.updated_at DESC;
```

After correcting the API key, billing, model access, or provider incident, an administrator with
`knowledge.review` access to the work item's knowledge base can use
`POST /api/v1/admin/jobs/{job_id}/retry` with an `Idempotency-Key` header. The operation reactivates
the same failed `JobIntent`, resets its attempt budget, writes an audit event, and emits a durable
Outbox request. It does not create another classification job. Unauthorized or cross-organization
jobs return `404`; non-failed or already-classified work returns `409`.

For historical duplicate chains, do not bulk-update job rows. Deploy this fix first: scheduled
workers will terminate non-authoritative pending duplicates as `EMAIL_CLASSIFICATION_SUPERSEDED`
without model calls. Use the administrator retry endpoint only on the oldest failed authoritative
job after the underlying cause is corrected. Keep the SQL output with the incident record, monitor
classification call counts and queue depth, and escalate any work item that still has more than one
non-terminal classification intent.

No Task 17 operation approves, sends, or retries Gmail delivery.
