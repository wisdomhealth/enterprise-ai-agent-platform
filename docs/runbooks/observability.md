# Production observability and readiness

This runbook covers the production process baseline. PostgreSQL is the durable
authority. Redis and broker messages are notification hints; losing Redis may
degrade live updates but must not make persisted work unrecoverable.
The S0 identity, audit, Outbox, authorization, and JobIntent foundations remain
the source of actor, event, and recovery truth for every subsystem.

## Endpoints

- `/health/live` is process-only. It never waits for a provider or database.
- `/health/ready` returns `503` when PostgreSQL is unavailable, the schema is not
  at `0021_webhook_subscriptions`, required key wrapping is unavailable, or the
  configured post-restore erasure generation is incomplete. It returns `200`
  with `status=degraded` when Redis, Claude, Drive, or Gmail is unavailable.
- `/metrics` exposes aggregate operational metadata only. It does not include
  prompts, answers, document text, email/chat bodies, credentials, customer
  identifiers, URLs, or tenant labels.

Set `RESTORE_GENERATION=0` during ordinary operation. After restoring a backup,
set it to a new positive generation, run `scripts/replay-erasure-ledger`, and do
not route customer traffic until `/health/ready` confirms `erasure_replay=up`.

## Database or required readiness failure

1. Keep traffic away from the backend while readiness is `503`; liveness may
   remain healthy.
2. Check PostgreSQL availability and the single Alembic head. Never edit the
   `alembic_version` row manually.
3. If `migrations=down`, run the approved migration release job, then recheck.
4. If `key_wrapping=down`, restore KMS access or the explicitly approved
   self-hosted 32-byte file key. Never copy plaintext connector secrets.

## Erasure replay

Run `scripts/replay-erasure-ledger --database-url "$DATABASE_URL"
--restore-generation "$RESTORE_GENERATION" --check`. If incomplete, run the
same command without `--check`; verify it from a fresh database session before
admitting traffic. The full procedure is in
[`data-erasure.md`](data-erasure.md#restore-replay-and-readiness). Do not clear
or fabricate ledger state to make readiness pass.

## Connector staleness

`PlatformDriveSyncStale` fires after staleness exceeds 30 minutes. Check the
durable sync cursor, connector authorization status, failed JobIntent records,
and Outbox delivery. Retry only approved retryable jobs. Credential failure
requires the existing explicit administrator reauthorization flow.

## Model errors

For `PlatformModelErrorsSustained`, inspect safe error codes and provider health.
Do not log or export prompts, retrieved chunks, answers, or provider bodies.
Groundedness, citation, authorization, and revocation gates remain fail closed.

## Expired job leases

For `PlatformExpiredJobLease`, confirm the periodic recovery worker is running.
The replacement worker must acquire the expired lease through PostgreSQL. Never
manually change lease owner, generation, expiry, or a terminal job state.

## Support backlog

For `PlatformSupportBacklog`, confirm support staffing and the durable queue.
Do not resume AI automatically: only the approved explicit human action may
leave `HUMAN_ACTIVE`.

## Delivery unknown

`PlatformDeliveryUnknownOld` is critical after 15 minutes. Reconcile through
the approved Gmail history/search workflow. `DELIVERY_UNKNOWN` never authorizes
a blind resend.

## Logs and dashboards

Application and worker output is structured JSON. The logging processor redacts
credential-like keys and content-bearing fields recursively. Loki/Promtail is a
transport and query layer, not a durable business authority. The provisioned
Grafana dashboard shows request, backlog, connector, ambiguous-delivery, and
erasure aggregates. Configure Alertmanager routing in the deployment secret
management system; the checked-in receiver intentionally has no external sink.

## TLS and SSE

Provision `tls.crt` and `tls.key` in `TLS_CERT_DIR` before starting Nginx. Nginx
redirects HTTP to HTTPS, enforces body/time limits and security headers, does not
follow an application redirect, and disables proxy buffering/cache for API/SSE
traffic so durable PostgreSQL replay remains the reconnect authority.

Before release run `docker compose config`, `make check-prometheus`, and
`scripts/check-operability --compose-file compose.yaml`. Each S0-S7 subsystem
must report a health endpoint, metrics, failure visibility, and this runbook.
