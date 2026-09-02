# Administrator operations

Use `/staff/admin` only with an active administrator session. PostgreSQL is the
source of truth for connector, synchronization, job, support, email, user, and
quality status. Times are displayed in UTC and errors are displayed only as safe
codes; the console never projects document, chat, email, credential, provider, or
job-payload bodies.

## Safe recovery

- Drive failures use **Retry Drive sync**. The existing durable job identity and
  payload are retained and the Drive domain publishes the normal sync Outbox event.
- `DELIVERY_UNKNOWN` uses **Reconcile Gmail**. Never retry or resend until the
  existing Gmail reconciliation workflow proves the message absent.
- Gmail `SEND_RETRY_WAIT` uses the existing email delivery state machine. A retry
  never mutates `JobIntent` generically.
- Connector reauthorization first validates the existing connector grant, then
  starts the Task 6 Google OAuth flow with only `drive.readonly`, or
  `gmail.readonly` plus `gmail.send`. The callback rotates the envelope-encrypted
  refresh token; the console never receives credentials.

## User administration

Invitations, role changes, and disables require an idempotency key and current
resource version. A stale version returns `409 RESOURCE_VERSION_CONFLICT` with the
current status and version. Disabling a user revokes every active staff session in
the same transaction. Each successful action records a redacted Audit event and a
transactional Outbox event.
