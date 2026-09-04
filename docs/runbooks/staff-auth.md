# Staff authentication runbook

## Purpose

The staff web application uses Google OpenID Connect only to establish a staff identity.
Admission is invitation-gated: an administrator must first create a `staff_users` record with
the staff member's exact Google email address and `INVITED` status. The first successful login
binds Google's stable `sub` claim to that record and changes the status to `ACTIVE`.

Google login tokens are never stored as staff sessions and must never be reused for Drive or
Gmail. Connector authorization uses separate client registrations, scopes, and credentials.

## Required configuration

- `DATABASE_URL`: PostgreSQL connection used for invitations and server-side sessions.
- `GOOGLE_OIDC_CLIENT_ID`: client ID for the staff-login OIDC application.
- `GOOGLE_OIDC_CLIENT_SECRET`: client secret for the staff-login OIDC application.
- `SESSION_SECRET`: high-entropy secret for the short-lived, signed OIDC flow cookie.
- `PUBLIC_BASE_URL`: externally reachable HTTPS origin. Google must allow
  `<PUBLIC_BASE_URL>/api/v1/auth/callback` as a redirect URI.
- `STAFF_SESSION_TTL_SECONDS`: optional server session lifetime; defaults to 28,800 seconds.

Do not place actual credentials in source control. If any required OIDC value is absent, the
login endpoint responds with `503` rather than inventing a fallback credential.

## Flow and endpoints

1. `GET /api/v1/auth/login` starts Authlib's authorization-code flow. It requests exactly
   `openid email profile`, uses PKCE `S256`, and includes a nonce.
2. `GET /api/v1/auth/callback` validates the OIDC response, requires a verified email, and
   admits only one exact matching, non-disabled invitation. A previously bound record must
   retain the same stable subject.
3. A successful callback creates a database `staff_sessions` row. The `staff_session` cookie
   contains only its random opaque ID and is `HttpOnly`, `Secure`, and `SameSite=Lax`.
4. `GET /api/v1/auth/me` resolves that opaque ID to the active staff principal. Missing,
   malformed, expired, revoked, and disabled-user sessions return `401`.
5. `POST /api/v1/auth/logout` requires the `X-CSRF-Token` header to match the token supplied in
   the readable `staff_csrf` cookie. The server compares its SHA-256 hash to `csrf_hash`, revokes
   the database session, and expires both cookies. Missing or incorrect CSRF values return `403`.

## Invitation and incident operations

- Store the invitation email exactly as returned by the intended Google account. Matching is
  deliberately case-sensitive and no domain-wide auto-admission exists.
- Set a staff user's status to `DISABLED` for immediate rejection of all of that user's current
  sessions. Set an individual session's `revoked_at` to revoke only that browser session.
- Treat a stable-subject mismatch as an account-integrity event. Do not overwrite the bound
  subject; verify the invitation and Google account before any deliberate administrative repair.
- Apply migrations through `0002_identity_sessions` before enabling the routes.

### Downgrading revision 0002

Revision `0001` requires every `staff_users.oidc_subject` value to be non-null, so migration `0002`
refuses to downgrade while any unbound invitation exists. The refusal happens before the session
table is dropped and leaves both the schema revision and identity data unchanged. It never invents
a subject and never deletes an invitation.

The safest response is to remain on `0002`. If rollback is operationally mandatory, first back up
the database and resolve every unbound invitation through an authorized process: let the intended
user complete verified Google login so the stable subject is bound, or explicitly cancel/archive
an invalid invitation according to the organization's retention policy. Confirm there are no
`staff_users` rows with a null `oidc_subject`, then retry the downgrade. Do not populate placeholder
subjects merely to bypass the guard.

## Local verification limitation

Automated tests inject a fake OIDC client and validated claims. They verify admission, cookie,
session, and CSRF behavior without making Google requests. A real end-to-end Google login still
requires deployment-specific HTTPS redirect registration and credentials; those values are not
part of the repository or local test suite.
