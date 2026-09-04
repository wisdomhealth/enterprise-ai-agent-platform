# Google connector credentials

Google Drive and Gmail refresh tokens are envelope encrypted before they reach
PostgreSQL. Each token receives a unique AES-256-GCM data key; PostgreSQL stores
only ciphertext, nonce, encrypted data key, algorithm, and KMS key version.

Production uses `GOOGLE_KMS_KEY_NAME` from the independently managed Google
Cloud KMS key. Do not place a production master key in PostgreSQL or an
environment value. `CONNECTOR_FILE_KEY_PATH` is accepted only when
`APP_ENV=development` or `SELF_HOSTED_FILE_KEY_ALLOWED=true`; it must name a
32-byte host-managed key file.

Configure `GOOGLE_DRIVE_CLIENT_ID` / `GOOGLE_DRIVE_CLIENT_SECRET` and
`GOOGLE_GMAIL_CLIENT_ID` / `GOOGLE_GMAIL_CLIENT_SECRET` in the deployment's
secret manager. No OAuth credential is committed to this repository. Only an
administrator may start, complete, reauthorize, or revoke a connector. OAuth
state is session-bound and single-use. Token values and provider responses must
never be placed in logs, audit details, or Outbox payloads.

The Drive connector requests only `drive.readonly`; the Gmail connector requests
only `gmail.readonly` and `gmail.send` for the later draft/review workflow. Rotate the KMS key
according to the KMS provider procedure, then reauthorize each connector to
write a newly wrapped refresh token.
