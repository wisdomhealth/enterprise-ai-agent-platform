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

Drive metadata and folder scope are assessed before the database write transaction.
If a file is deleted, trashed, loses file access, or leaves the freshly enumerated
authorized folder tree, one transaction invalidates its parse tasks, marks every
non-deleted version `REVOKED`, clears the current retrievable version, deletes only
those versions' `document_chunks` (including vectors), writes a scoped audit event,
and advances the cursor. Already-revoked versions with remaining chunks are included.
Temporary Drive failures, OAuth failures, and incomplete pages do not apply cleanup
or advance the cursor. Document and version records remain as lifecycle history.

Historical `knowledge.document.cleanup.requested` events are still consumed
idempotently for deployments that created them before this change. New syncs do not
create or wait for a separate cleanup event.

If the authorized root itself is trashed, removed, inaccessible, or explicitly
denied, all documents owned by that source are cleaned through the same transaction
and the source becomes `DISABLED`. The job error records the exact
`DRIVE_ROOT_UNAVAILABLE_*` reason. This differs from OAuth expiry (`ERROR` plus a
`REAUTH_REQUIRED` connector) and from an administrator's intentional disablement.
The worker never changes a disabled source back to `ACTIVE` on its own.

To restore a root-disabled source, first restore the original root and the connector
identity's access, then use **Save Drive scope** to validate and save that root again;
or save a different accessible root. A successful administrator configuration is
the explicit action that reactivates the source. Trigger synchronization afterwards.

## Replace previously ingested files from the UI

Use this workflow when replacing the old corpus after deploying the LlamaIndex
ingestion adapters:

1. Keep the configured Drive root folder and any required subfolders.
2. Delete or move the old files to trash in Google Drive. Do not delete the root
   folder itself.
3. In the staff UI, open the knowledge source and trigger synchronization.
4. Wait until the sync reports success. At that point the old versions are
   `REVOKED` and their chunks are physically absent; there is no second cleanup
   queue to wait for.
5. Upload the replacement files. They must receive new Google Drive file IDs;
   reusing the same filename is safe and does not reuse the old document record.
6. Trigger synchronization again and wait for parsing/embedding to finish. The new
   version becomes retrievable only after every chunk and vector validates and the
   publication transaction switches `current_version_id`.

Do not clear database tables, edit cursors, invoke bulk ingestion scripts, or delete
document/version records. A failed new sync transaction leaves its cursor and data
unchanged and the durable sync job remains retryable. For a historical cleanup
event, leave the event in place and restore the worker/broker/database dependency;
the scheduled dispatcher retries it.
