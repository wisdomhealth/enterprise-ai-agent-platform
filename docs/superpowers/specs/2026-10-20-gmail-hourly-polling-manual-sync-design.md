# Gmail Hourly Polling and Manual Sync Design

## Goal

Add independent hourly Gmail polling and a visible administrator-only manual synchronization control while retaining Gmail Push. Scheduled, manual, and push triggers must create work through one durable synchronization coordinator and the existing history ingestion worker.

## Configuration

- Add `GMAIL_POLLING_ENABLED`, defaulting to `false` in application settings and enabled in local Compose configuration.
- Reuse `GMAIL_RECONCILIATION_INTERVAL_SECONDS` as the mailbox due interval. Set the documented local default to `3600` seconds.
- Do not add `GMAIL_POLL_INTERVAL_SECONDS`; this avoids two interval settings and preserves compatibility with the existing name.
- Celery Beat keeps one Gmail polling entry and invokes it every 60 seconds. The task only creates work for a mailbox whose last successful sync is older than the reconciliation interval.
- `GMAIL_PUSH_ENABLED` controls only watch renewal, webhook availability, and push receipt maintenance. It does not gate polling or manual synchronization.

## Shared Scheduling

Introduce one coordinator used by polling, manual recovery operations, and push acceptance. It locks `EmailSyncState`, validates organization and state, checks `active_sync_job_id`, ignores terminal stale references, increments `sync_generation`, and atomically writes the `JobIntent`, Outbox event, active-job reference, and trigger source.

The coordinator returns the existing active job for duplicate requests. It refuses automatic or manual activation while the state is `PAUSED_ERROR`, `RECOVERY_REQUIRED`, or `RECOVERY_TRUNCATED`; existing recovery operations remain the only way to resume bounded recovery. Push notifications may still persist their receipt and high-water mark while work is paused.

## Success and Due Time

`EmailSyncState.last_success_at` remains the single polling time baseline. It is updated only after the history worker has committed all business records and downstream durable intents and successfully completes the sync job. Empty incremental history is a successful synchronization and updates this timestamp. Accepted, queued, running, retried, and failed work never update it.

Successful push and manual runs therefore postpone the next scheduled poll. Scheduler scans do not create catch-up jobs for each missed interval and never replace a retrying job.

## API and Authorization

Reuse `POST /api/v1/admin/connectors/{connector_id}/gmail-sync/manual`. It remains protected by administrator session, CSRF, organization scoping, optimistic version validation, and idempotency. The response exposes the current active job ID/state/trigger plus last successful synchronization time so acceptance is not presented as completion.

Manual sync does not require Push, Pub/Sub configuration, a public webhook, or an active Watch. It requires an active Gmail connector and a non-paused synchronization state.

## Staff UI

The existing `/staff/email` page renders synchronization controls only for administrators. Multiple Gmail connectors are selectable. The control distinguishes queued/running/succeeded/failed or recovery states, displays polling and Push status independently, and polls the existing state endpoint until the accepted job finishes. The email queue refreshes only after observed successful completion. Copy explains that message classification and draft generation can remain pending after mailbox synchronization.

## Safety

The history worker continues to use persisted cursors, bounded initialization, expired-cursor recovery, Gmail message uniqueness, job leases, generation fencing, and transactional Outbox records. It does not restart failed classification/draft jobs and never sends mail. Gmail API calls remain outside the short scheduling transaction.

## Testing

Regression tests cover configuration independence, a single scheduler entry, due-time checks, duplicate and cross-trigger synchronization, paused states, administrator authorization, durable Outbox delivery, empty-history success, exact-once ingestion, and UI completion reporting. Tests use the isolated PostgreSQL fixture and fake Gmail gateways only.
