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

Each Gmail page, its normalized work items, classification provenance, retry intents, Outbox
events, and the corresponding history/page cursor are committed together. A failed page rolls back
the entire page and leaves the previous cursor resumable. The bootstrap history anchor is retained
across all bootstrap pages so messages arriving during the initial scan are collected by the next
history poll. `(organization_id, gmail_message_id)` prevents duplicate work items.

The stored raw reference is an opaque `gmail://` locator, never raw MIME content or an access token.
Sender, recipients, subject, plain-text body, thread ID, history ID, and received timestamp are
normalized before persistence.

## Classification and drafting

Claude classification accepts only the exact category values `ACTION_REQUIRED`, `INFORMATIONAL`,
`SPAM`, `UNKNOWN`; priority values `HIGH`, `NORMAL`, `LOW`; and a required boolean
`reply_required`. Extra, missing, inconsistent, or unknown fields fail closed.

`ACTION_REQUIRED` and `UNKNOWN` enter `DRAFTING` and receive a durable draft job. `INFORMATIONAL`
and `SPAM` stay visible as ingested work without an automatic send path. Draft generation uses the
staff RAG audience and persists only organization- and knowledge-base-authorized citations plus
model, prompt, retrieval, latency, token, and estimated-cost provenance. Successful drafts stop at
`AWAITING_REVIEW`.

Classification or draft failures move the item to `DRAFT_RETRY_WAIT`, store a safe error code, and
create a complete retry `JobIntent`. Provider response bodies and exception text are not persisted.
An operator retry must call `EmailIngestionService.retry_failed_work_item`; direct state edits are
not supported.

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

No Task 17 operation approves, sends, or retries Gmail delivery.
