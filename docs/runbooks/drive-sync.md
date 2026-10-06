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
Moving a file to Drive trash and permanently deleting it use this same path.
Temporary Drive API failures fail the sync job and never masquerade as deletion.

The transactional cleanup event records the organization, source, document, and
exact version IDs revoked by that sync operation. A Celery worker validates those
relationships and states before deleting only those versions' `document_chunks`;
the vector stored in each row disappears with the row. The document and version
records remain for lifecycle and audit history. Delivery and consumption are
idempotent, with a one-minute pending-event sweep and bounded task retries.

## Replace previously ingested files from the UI

Use this workflow when replacing the old corpus after deploying the LlamaIndex
ingestion adapters:

1. Keep the configured Drive root folder and any required subfolders.
2. Delete or move the old files to trash in Google Drive. Do not delete the root
   folder itself.
3. In the staff UI, open the knowledge source and trigger synchronization.
4. Wait until the source status reports a successful sync and no cleanup backlog.
   At that point the old versions are `REVOKED` and their chunks are physically
   absent.
5. Upload the replacement files. They must receive new Google Drive file IDs;
   reusing the same filename is safe and does not reuse the old document record.
6. Trigger synchronization again and wait for parsing/embedding to finish. The new
   version becomes retrievable only after every chunk and vector validates and the
   publication transaction switches `current_version_id`.

Do not clear database tables, edit cursors, invoke bulk ingestion scripts, or delete
document/version records. If cleanup fails, leave the Outbox event in place and
restore the worker/broker/database dependency; the scheduled dispatcher retries it.
