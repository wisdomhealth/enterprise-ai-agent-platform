# Durable jobs, Outbox, and audit operations

## Database roles and migrations

Run Alembic with `MIGRATION_DATABASE_URL`. This connection must use the database owner
role and is the only runtime configuration allowed to perform schema changes. API and
worker processes use `DATABASE_URL` with the `platform_app` role.

The Task 5 migration grants `platform_app` normal DML access to durable workflow tables.
For `audit_events`, it grants only `SELECT` and `INSERT`; PostgreSQL rejects `UPDATE` and
`DELETE`. Audit rows must be corrected by appending a new event rather than mutating
history.

## Transactional Outbox

Write the business record and call `OutboxService.add()` with the same `AsyncSession`.
Commit once. A rollback removes both changes. The dispatcher reads unpublished rows with
`FOR UPDATE SKIP LOCKED`, increments `publish_attempts`, delivers the event, and only then
sets `published_at`. A crash after delivery but before commit causes a safe duplicate.

Consumers must call `OutboxService.begin_processing()` in the same transaction as their
side effects. The `(consumer_name, event_id)` primary key returns `False` for duplicates.
Do not use Redis or Celery result state as proof that an event was processed.

## Job leases and recovery

`JobService.enqueue()` deduplicates on `(kind, idempotency_key)`. A worker calls
`JobLeaseService.claim()` before work. Claim is one conditional `UPDATE ... RETURNING`:
only due pending work or a running job with an expired lease can transition to `RUNNING`.
Payload is retained during takeover and `attempts` is incremented. Lease comparisons,
expiry values, completion, and retry scheduling use PostgreSQL's clock rather than a worker's
wall clock.

Failure routing is durable:

- `RETRYABLE`: return to `PENDING` with jittered exponential backoff. A provider
  `Retry-After` value is a minimum delay.
- `NON_RETRYABLE`: move to terminal `FAILED`.
- `AMBIGUOUS`: move to `RECONCILIATION`; never blindly repeat an uncertain side effect.
- `SECURITY`: deny the operation, move to `FAILED`, and append a redacted audit signal.

Manual and automated retries call the same `JobLeaseService.retry()` path. Celery is only
a scheduling hint; PostgreSQL job state and leases are authoritative. Worker failure entry
points require organization and actor IDs so a SECURITY transition always writes its redacted
audit signal in the same transaction.

## Idempotent write fencing

Every `IdempotencyService.begin()` acquisition has a durable UUID `lease_token`. An expired
takeover replaces the token. The executor must pass the token returned by `begin()` to
`complete()`; a stale or expired token raises `IdempotencyLeaseLost` and cannot overwrite the
new executor's result. Completed matching requests replay their stored safe response.

Response persistence is deny-by-default. Without `safe_response_keys`, the stored body is
empty. Callers may explicitly allow-list fields that are safe to replay; secrets, credentials,
and provider bodies must never be allow-listed.

## Recovery checklist

1. Confirm PostgreSQL is healthy and the current Alembic revision is
   `0005_audit_outbox_jobs`.
2. Inspect overdue `RUNNING` rows by `lease_expires_at`; restarting workers is safe because
   an expired row can be reclaimed.
3. Inspect pending Outbox rows by `published_at IS NULL` and `publish_attempts`; restart the
   dispatcher after resolving broker availability.
4. Investigate `RECONCILIATION` jobs using domain state before choosing a terminal or retry
   outcome.
5. Never repair audit history with DML from the application role. Append a corrective event
   through an authorized operational path.
