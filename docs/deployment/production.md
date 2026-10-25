# Production deployment

## Preconditions

The customer owns the production cloud project, networking, DNS, TLS certificate,
secret manager/KMS, provider accounts, and backup storage. This repository contains
only empty placeholders in `.env.example`. Supply values at deployment time through
the approved secret manager; do not place values in Git, evidence JSON, shell
history, tickets, dashboards, or support messages.

Use a KMS resource identifier in `GOOGLE_KMS_KEY_NAME`, for example a customer-owned
key reference, not a key value. Use environment references such as `DATABASE_URL`,
`PGBACKREST_REPO1_S3_KEY_SECRET` and `OPENAI_API_KEY`; never
replace them with real credentials in this document.

## Reproducible release procedure

1. Select an approved immutable repository tag and matching scanned container image
   digests. Record both in the change ticket and asset register.
2. Have the customer provision the environment registry in
   [credential ownership](credential-ownership.md), TLS material at an explicit absolute
   `TLS_CERT_DIR`,
   PostgreSQL/pgBackRest storage, and network policy (including internal-only metrics).
3. Validate the certificate and matching private key, then render and inspect the
   production definition before starting it:

   ```bash
   TLS_CERT_DIR=/run/platform-tls scripts/check-tls-material
   TLS_CERT_DIR=/run/platform-tls docker compose \
     -f compose.yaml -f compose.production.yaml config
   make check-prometheus
   scripts/check-operability --compose-file compose.yaml
   ```

   `compose.production.yaml` requires `TLS_CERT_DIR`; an unset value fails interpolation.
   Missing, unreadable, invalid, or mismatched `tls.crt` and `tls.key` fail the preflight.
   Nginx mounts the external directory read-only. Production must not use the repository
   path or the ignored local-development default.

4. Run the migration operation with the separate `MIGRATION_DATABASE_URL`, then
   start the Compose services with both files:

   ```bash
   TLS_CERT_DIR=/run/platform-tls docker compose \
     -f compose.yaml -f compose.production.yaml up -d
   ```

   Do not reuse migration credentials for the application role.
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

The localhost private key formerly committed at `infra/nginx/tls/tls.key` is exposed
and is prohibited for every new deployment. Removing it from the current tree does not
remove it from Git history. Confirm independently whether any environment ever used it;
if usage cannot be disproved, rotate the deployed certificate and private key through
the environment owner. See the [exposure record](../operations/tls-key-exposure.md).

## Change and rollback

Use [change control](../scope/change-control.md) for every production change. Only
roll back application images when the migration compatibility plan permits it;
published migrations are append-only. For data restore, use the guarded procedure in
[backup recovery](../runbooks/backup-recovery.md), a new empty target and a new
`RESTORE_GENERATION`; never overwrite a live data directory.

This AI framework migration has no database migration and does not require
re-embedding while `OPENAI_EMBEDDING_MODEL` remains unchanged. Rollback uses the
previous application image and its previous provider configuration as one reviewed
release unit; never mix old runtime settings with the new image. There is no automatic
provider fallback. Verify rollback with local fake providers before restoring traffic.
