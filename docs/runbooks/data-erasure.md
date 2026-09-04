# Data retention and erasure

Retention periods are product defaults, not legal or regulatory guarantees.
Each customer must choose periods with appropriate business and legal advice.

## Retention policy

Administrators with an explicit retention-resource grant can update the
organization's chat, email, and audit periods through the administrator
console. Updates use an expected version and an idempotency key, and emit safe
audit and Outbox evidence. The initial defaults are 90 days for chat content,
90 days for email content, and 365 days for audit events.

The daily durable job redacts expired chat and email bodies, customer contact
fields, generated drafts, citations, and derived snapshots in bounded,
idempotent batches. It preserves identifiers and state needed for minimal
operational evidence. These defaults never delete Drive documents, document
versions, chunks, or vectors; those records are removed only by existing Drive
revocation/deletion handling or an explicitly authorized knowledge-document
erasure request.

## Erasure request and evidence

Set `ERASURE_HASH_KEY` from the production secret manager. It is a dedicated
key used to HMAC normalized subject references and must not be stored in
PostgreSQL. Rotate it only with a documented re-keying procedure because ledger
matching depends on stable hashes.

An authorized administrator creates a request with an idempotency key. The
ledger stores the keyed subject hash, scope, timestamps, state, replay
generation, target identifiers, safe error code, and verification counts. It
never stores deleted bodies, OAuth values, authorization headers, document
content, email content, or chat content. Audit and Outbox records contain only
request/job identifiers and safe state/count metadata.

The `retention.erasure.apply` durable job performs domain deletion. Check its
JobIntent and the request's verification counts before closing an operational
request. A failed job must be retried through the normal fenced JobIntent
recovery path.

## Restore replay and readiness

After restoring an older PostgreSQL backup, keep public and staff readiness
blocked. Restore the authoritative erasure ledger material, apply current
migrations, and then run:

```bash
scripts/replay-erasure-ledger \
  --database-url "$DATABASE_URL" \
  --restore-generation "$RESTORE_GENERATION"
```

The command reapplies every request whose recorded replay generation is older
than the current restore generation and commits the idempotent result. Verify
the gate independently:

```bash
scripts/replay-erasure-ledger \
  --database-url "$DATABASE_URL" \
  --restore-generation "$RESTORE_GENERATION" \
  --check
```

Exit status zero means every ledger entry is applied for that generation.
Any non-zero result keeps readiness blocked. Record the restore generation,
request count, verification counts, command result, operator, and timestamp as
recovery evidence before reopening service. Old backups expire through the
configured backup lifecycle; do not mutate backup archives in place.
