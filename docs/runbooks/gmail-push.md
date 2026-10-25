# Gmail incremental synchronization

## Deployment status

Scheduled polling, administrator manual synchronization, and Gmail Push share one incremental
history pipeline. Local Compose uses `GMAIL_POLLING_ENABLED=true`,
`GMAIL_RECONCILIATION_INTERVAL_SECONDS=3600`, and `GMAIL_PUSH_ENABLED=false`. The scheduler scans
once per minute but calls Gmail only by creating work for a mailbox whose last successful sync is
at least one configured interval old.

`GMAIL_RECONCILIATION_INTERVAL_SECONDS` is the supported polling due interval. No separate
`GMAIL_POLL_INTERVAL_SECONDS` is used, so existing deployments keep one compatible setting and one
scheduler entry. `GMAIL_POLLING_ENABLED` and `GMAIL_PUSH_ENABLED` are independent. Missing Push
configuration does not disable polling or administrator manual synchronization. When Push is
enabled with a required setting missing, application startup emits the safe
`gmail_push_configuration_incomplete` diagnostic with configuration names only; the webhook and
administrator watch actions remain unavailable until configuration is complete.

## Polling configuration

- `GMAIL_POLLING_ENABLED=true`
- `GMAIL_RECONCILIATION_INTERVAL_SECONDS=3600`
- Valid `GOOGLE_GMAIL_CLIENT_ID`, `GOOGLE_GMAIL_CLIENT_SECRET`, and an authorized Gmail connector

The Beat task runs every 60 seconds only to check due timestamps. Successful manual or Push syncs
update the same `last_success_at` baseline. Queue acceptance, job start, retry, and failure do not.
Missed hours never create one job per interval.

## Optional Push configuration

- `GMAIL_PUSH_ENABLED=true`
- `GMAIL_PUBSUB_TOPIC=projects/<project>/topics/<topic>`
- `GMAIL_PUBSUB_SUBSCRIPTION=projects/<project>/subscriptions/<subscription>`
- `GMAIL_PUSH_ENDPOINT_URL=https://<public-host>/api/v1/webhooks/gmail/pubsub`
- `GMAIL_PUSH_OIDC_AUDIENCE` equal to the configured push audience
- `GMAIL_PUSH_SERVICE_ACCOUNT_EMAIL` equal to the dedicated push authentication service account
- `GOOGLE_GMAIL_CLIENT_ID` and `GOOGLE_GMAIL_CLIENT_SECRET`

Operational defaults are `GMAIL_POLLING_ENABLED=false`,
`GMAIL_RECONCILIATION_INTERVAL_SECONDS=3600`,
`GMAIL_WATCH_RENEWAL_INTERVAL_SECONDS=86400`,
`GMAIL_WATCH_RENEW_BEFORE_SECONDS=172800`,
`GMAIL_PUSH_MAX_BODY_BYTES=65536`, `GMAIL_PUSH_RECEIPT_RETENTION_DAYS=14`,
`GMAIL_RECOVERY_LOOKBACK_DAYS=7`, and `GMAIL_RECOVERY_MAX_MESSAGES=500`. A history job receives at most
`GMAIL_SYNC_MAX_ATTEMPTS=5` executions before automatic synchronization pauses with pending work
preserved for administrator remediation. Each automatic draft retry cycle includes at most
`EMAIL_DRAFT_MAX_ATTEMPTS=3` executions, including its first execution.

`NEXT_PUBLIC_LIVENESS_URL` is embedded during the frontend build. Changing it requires rebuilding
the frontend image. Local development defaults to `http://localhost/health/live`; production must
set the externally reachable health URL.

## Google and Pub/Sub IAM boundaries

1. Grant the Gmail API publisher identity permission to publish only to the configured topic.
2. Allow the Pub/Sub service agent to operate the subscription as required by Google Cloud.
3. Configure the push subscription to mint an OIDC token for the dedicated push service account.
4. Grant a managed-service Invoker role only when the actual hosting platform requires it.

The push service account, Pub/Sub service agent, and Gmail publisher are distinct principals.
Application access grants and Google OAuth/Pub/Sub IAM are also administered separately.

## Runtime flow

`users.watch` sends `topicName`, `labelIds=["INBOX"]`, and
`labelFilterBehavior="include"`. Notifications are accepted only after JWT signature, issuer,
expiration, audience, stable subject, verified service-account email, subscription, mailbox, and
numeric history ID validation.

The scheduler, manual endpoint, and webhook lock the same mailbox state and use the same
generation-fenced coordinator. The webhook commits receipt, pending numeric high-water, canonical `email.gmail_history` job, and
`email.gmail_history.requested` Outbox event together. It then returns 204. The dispatcher marks
`published_at` only after Celery accepts the job. Duplicate delivery is safe through the receipt,
JobIntent lease, Gmail message uniqueness, cursor, and business state.

When the processed cursor is empty or expired, recovery records baseline H0 before scanning the
last seven days (up to 500 messages), then catches up from H0. Reaching the cap is a truncated
recovery, not a complete recovery. An administrator must continue or explicitly accept the limited
scope; accepting never skips the H0 catch-up.

Administrator operations are exposed under `${PUBLIC_BASE_URL}/api/v1/admin`:

- `GET /gmail-sync` reads watch, synchronization, pending cursor, recovery, and safe error state.
- `POST /connectors/{connector_id}/gmail-watch/{start|renew|stop}` changes the watch lifecycle.
- `POST /connectors/{connector_id}/gmail-sync/manual` starts an authorized manual synchronization.
- `POST /connectors/{connector_id}/gmail-recovery/continue` increases a truncated recovery bound.
- `POST /connectors/{connector_id}/gmail-recovery/accept-limited` records explicit limited-recovery acceptance and still schedules H0 catch-up.
- `POST /connectors/{connector_id}/gmail-recovery/resume` resumes a synchronization paused by `GMAIL_REAUTH_REQUIRED` after OAuth succeeds.

All mutating operations require an active same-organization administrator, CSRF protection,
optimistic state validation where applicable, and an `Idempotency-Key`.

Manual synchronization works with Push disabled, Watch stopped, and no Pub/Sub configuration. It
returns after durable acceptance. The status response exposes the active job and trigger; only a
later successful worker completion updates `last_success_at`. `PAUSED_ERROR`, `RECOVERY_REQUIRED`,
and `RECOVERY_TRUNCATED` require the existing authorized reauthorization or recovery operations.
For `GMAIL_REAUTH_REQUIRED`, first use the existing connector reauthorization endpoint and complete
Google OAuth. Reauthorization rotates credentials but intentionally preserves the paused sync state;
an administrator must then use the recovery resume action. Resume preserves Gmail cursors, creates
only a generation-fenced Gmail History job, and does not restart failed classification or draft jobs.

## Migration and deployment sequence

1. Back up PostgreSQL and obtain the required operator approval. Development does not run this
   migration or historical repair against a real deployment automatically.
2. Configure polling and/or Push, Google OAuth, public base URL, database, broker, and
   connector-encryption settings through the deployment secret/configuration system. Build the
   frontend with the final `NEXT_PUBLIC_LIVENESS_URL`.
3. Inspect the rendered Compose configuration, then run the repository's migration operation:
   `docker compose --profile operations run --rm migrate`. This upgrades to
   `0022_gmail_push_access_draft_reliability` and preserves the existing processed Gmail cursor.
4. Start or replace the reviewed `backend`, `worker`, `scheduler`, `frontend`, and ingress services
   with `docker compose up -d`. Push ingestion requires all three backend runtime roles; starting
   only the HTTP service is insufficient.
5. Verify liveness/readiness. From `/staff/email`, an administrator selects an authorized mailbox
   and presses **Sync emails**. Confirm one JobIntent and Outbox event, then observe the job complete,
   `last_success_at` advance, the email queue refresh, and classification/draft work remain separate.
   If Push is enabled, separately register or renew the Watch and verify a controlled notification.
6. Keep the previous running version in service if target-environment verification fails. A
   one-minute scan is expected; a one-minute Gmail API call or new History Job is not.

## Initial administrator follow-up

No bootstrap command is executable in this change. The next task will add a controlled one-time
CLI that atomically creates an organization and `INVITED/ADMIN` user, uses Google OIDC without a
password, requires an immutable organization identifier or explicit new-organization mode, and is
idempotent without promoting or resetting existing users. The current callback already requires a
verified exact email, binds the stable Google `sub`, activates a valid invitation, and rejects
mismatch, identity conflict, and disabled accounts. No public administrator-creation endpoint will
be added.
