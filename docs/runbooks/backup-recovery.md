# PostgreSQL backup and recovery

PostgreSQL is the recovery authority. Redis is only a notification and broker
accelerator; never restore application state from Redis. This runbook implements
the operational target of an observed recovery point no older than 15 minutes
and an observed recovery time under four hours. Those are measured targets, not
an untested SLA.

## Required image and secrets

Pin `POSTGRES_IMAGE` to an internally built and scanned PostgreSQL 17 image that
contains both pgvector and pgBackRest. The default development image keeps the
Compose file renderable but is not backup-ready unless it contains `pgbackrest`.
Verify both before deploying:

```bash
docker compose exec postgres postgres --version
docker compose exec postgres pgbackrest version
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE EXTENSION IF NOT EXISTS vector"
```

Inject these values from the production secret manager; do not commit them or
place them in recovery evidence:

- `PGBACKREST_REPO1_S3_BUCKET`, `PGBACKREST_REPO1_S3_ENDPOINT`, and region/TLS settings;
- `PGBACKREST_REPO1_S3_KEY` and `PGBACKREST_REPO1_S3_KEY_SECRET`;
- `PGBACKREST_REPO1_CIPHER_PASS` (separate from database and connector keys).

The repository uses AES-256-CBC encryption. Enforce bucket-side encryption,
versioning, object lock/immutability, least-privilege access, and an independently
managed lifecycle in the storage provider as well.

## Continuous archive and scheduled backups

`infra/postgres/postgresql.conf` enables continuous WAL archive through
pgBackRest and forces a segment boundary at least every 900 seconds. Alert on an
archive backlog or failed `pgbackrest check`.

Run a differential backup daily and a full backup weekly from an authenticated
operations scheduler. The scheduler must inject repository credentials only for
the process lifetime:

```bash
scripts/backup-postgres --type diff \
  --evidence docs/evidence/recovery/daily-$(date -u +%Y%m%dT%H%M%SZ).json

scripts/backup-postgres --type full \
  --evidence docs/evidence/recovery/weekly-$(date -u +%Y%m%dT%H%M%SZ).json
```

The wrapper creates/validates the stanza, checks archive connectivity, completes
the backup, reads the resulting label from pgBackRest, and atomically writes safe
evidence. A dry run writes no backup evidence.

## Point-in-time restore

Never restore over an existing data directory. Stop application traffic first,
choose a new empty target, choose an ISO-8601 timestamp with timezone, and assign
a positive, monotonically increasing restore generation. The confirmation value
must resolve to the exact target path:

```bash
scripts/restore-postgres \
  --target-volume /srv/platform/restores/generation-7 \
  --confirm-empty-target /srv/platform/restores/generation-7 \
  --target-timestamp 2026-08-09T12:00:00Z \
  --restore-generation 7 \
  --database-url "$DATABASE_URL" \
  --migration-database-url "$MIGRATION_DATABASE_URL" \
  --execute
```

The command refuses a missing, non-directory, non-empty, or differently confirmed
target. It writes a blocked restore-generation marker before starting. It restores
through pgBackRest, starts only the restored database, applies the current append-only
migrations, replays and verifies the erasure ledger, rebuilds pgvector/FTS indexes,
and then restarts API/workers with the new `RESTORE_GENERATION`. The API readiness
gate opens only after its migration and erasure checks pass. Any interrupted or
failed command leaves the marker blocked; investigate rather than reusing the target.

Preserve the authoritative erasure request and target records at or before the
chosen restore point. Restoring to a point before a deletion request was durably
recorded cannot reconstruct an intent that did not yet exist.

## Recovery drill

Use only the dedicated test Compose project and an empty test target. The execute
mode requires the exact project-name confirmation:

```bash
scripts/verify-recovery \
  --compose-file compose.test.yaml \
  --project-name task25-recovery \
  --confirm-disposable-project task25-recovery \
  --target-volume /tmp/task25-recovery-target \
  --execute \
  --evidence docs/evidence/recovery/local-verification.json
```

The drill must record the backup label, requested and actual restore points,
measured RPO/RTO, migration state, erasure replay, Redis-loss job recovery, and
final readiness. Evidence must state `sla_claimed: false`. If either measured
target is missed, retain the evidence, file an operational risk, and improve the
process before representing the target as met.

After the drill, inspect pending/expired `JobIntent` rows and unpublished Outbox
events. Restarting workers is safe because PostgreSQL leases and event identities
remain authoritative. Never infer completion from Celery or Redis state.
