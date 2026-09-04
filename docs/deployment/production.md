# Production deployment

## Preconditions

The customer owns the production cloud project, networking, DNS, TLS certificate,
secret manager/KMS, provider accounts, and backup storage. This repository contains
only empty placeholders in `.env.example`. Supply values at deployment time through
the approved secret manager; do not place values in Git, evidence JSON, shell
history, tickets, dashboards, or support messages.

Use a KMS resource identifier in `GOOGLE_KMS_KEY_NAME`, for example a customer-owned
key reference, not a key value. Use environment references such as `DATABASE_URL`,
`PGBACKREST_REPO1_S3_KEY_SECRET`, `ANTHROPIC_API_KEY`, and `OPENAI_API_KEY`; never
replace them with real credentials in this document.

## Reproducible release procedure

1. Select an approved immutable repository tag and matching scanned container image
   digests. Record both in the change ticket and asset register.
2. Have the customer provision the environment registry in
   [credential ownership](credential-ownership.md), TLS material at `TLS_CERT_DIR`,
   PostgreSQL/pgBackRest storage, and network policy (including internal-only metrics).
3. Render and inspect the production definition before starting it:

   ```bash
   docker compose config
   make check-prometheus
   scripts/check-operability --compose-file compose.yaml
   ```

4. Run the migration operation with the separate `MIGRATION_DATABASE_URL`, then
   start the Compose services. Do not reuse migration credentials for the application
   role.
5. Confirm `/health/live`, then `/health/ready`, Prometheus scrape, Grafana access,
   alert routing and one authorized staff login. Readiness must remain closed if the
   schema, key wrapping, or post-restore erasure replay is incomplete.
6. Perform and retain a backup/PITR drill using
   [backup recovery](../runbooks/backup-recovery.md). The measured targets are not an
   SLA. Run the release gates with local fakes only when customer provider credentials
   are unavailable; record the limitation in acceptance.

## Runtime components

`compose.yaml` contains PostgreSQL, Redis, backend, worker, scheduler, frontend,
Nginx, Prometheus, Alertmanager, Loki, Promtail and Grafana. PostgreSQL is durable
state. Redis is a notification/broker accelerator. Nginx terminates TLS and keeps
SSE unbuffered. Alertmanager has no checked-in external receiver: the customer must
configure and test the production route before acceptance.

## Change and rollback

Use [change control](../scope/change-control.md) for every production change. Only
roll back application images when the migration compatibility plan permits it;
published migrations are append-only. For data restore, use the guarded procedure in
[backup recovery](../runbooks/backup-recovery.md), a new empty target and a new
`RESTORE_GENERATION`; never overwrite a live data directory.
