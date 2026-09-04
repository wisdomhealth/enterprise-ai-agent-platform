# Google Drive knowledge-source sync

Drive changes are synchronized every 15 minutes. Manual requests use the same
durable sync intent and cursor key, so a duplicate request never creates a
second concurrent page application.

## Inspect a source

Use `GET /api/v1/admin/knowledge-sources/{source_id}/status` as an authorized
administrator. The response exposes the current cursor, the completion time
of the last durably successful sync intent, queue backlog, revoked/isolation
count, retry count, and recent safe error codes. A failed attempt is not
reported as a successful sync merely because it changed a source record. The
response deliberately never returns a Google token or connector secret.

## Safe retry

Use `POST /api/v1/admin/knowledge-sources/{source_id}/sync`. It enqueues the
same idempotent intent used by the periodic worker; do not run a direct Drive
script or alter a cursor by hand.

## Reauthorize Google Drive

An invalid or revoked Drive credential sets the connector to
`REAUTH_REQUIRED` and stops further ingestion. Reauthorize through the
existing authorized connector flow, then request a safe retry through the
sync endpoint. Do not put refresh tokens, client secrets, or authorization
headers in tickets, logs, or commands.

## Revocation behavior

If a file is deleted, leaves the allowed folder tree, or loses accessible
authorization, its document versions are immediately marked `REVOKED` and
the current retrievable version reference is cleared in the cursor transaction.
Physical chunks are removed asynchronously from the cleanup outbox event.
