# Staff identity and access

## Current sign-in and session behavior

Staff authentication uses Google OIDC SSO only. There is no local password database and no local
password reset flow. The public homepage submits to `/api/v1/auth/login`; externally the exact URL
is `${PUBLIC_BASE_URL}/api/v1/auth/login`. `PUBLIC_BASE_URL` must be an origin such as
`https://platform.example.com`. The callback is `${PUBLIC_BASE_URL}/api/v1/auth/callback`.

The callback accepts a Google identity only when its email is verified, exactly one existing
`INVITED` or `ACTIVE` staff record has the same normalized email, and any existing OIDC subject
binding matches the stable Google `sub`. A valid invited user is activated and bound on first
login. Disabled users, email mismatches, duplicate matches, and subject conflicts are rejected.

Successful login creates a server-side `staff_sessions` record plus secure session and CSRF
cookies. `GET /api/v1/auth/me` validates the current session. `POST /api/v1/auth/logout` revokes the
server-side session and removes both cookies. Disabling a staff user revokes all of that user's
active sessions immediately.

## Current user administration

An active administrator can list users, create an `INVITED` user, change a user's role, and change
account status through `/api/v1/admin/users`. Updates use optimistic versions, audit records, and
durable Outbox events. The administration page exposes those operations.

There is no application password recovery because the application has no local passwords. Google
account recovery remains outside this application's security boundary.

## Current application and resource authorization

Email and Support entry require a same-organization `ResourceGrant` for the deterministic
application resource plus the role/state policy. An application grant is only the first gate:
knowledge review, email approval/send, support claim/reply, connector management, and other
operations retain their existing fine-grained checks. `MEMBER` cannot gain review capability from
an application grant. Disabled accounts are denied even if grants remain.

The administrator authorization page can list and grant/revoke Email or Support application access
with optimistic concurrency, idempotency conflict detection, atomic audit, and exact display of
the user's role/status and reviewable knowledge-base IDs. Knowledge scope continues to use the
existing `ResourceGrant` rows; this change does not create a second assignment store or widen any
knowledge grant. Connector OAuth and Pub/Sub IAM remain separate from application authorization.

`GET /api/v1/staff/application-access` returns only the signed-in user's effective Email and
Support access. The frontend Email and Support route layouts check this endpoint on the server
before rendering. The Admin route layout instead requires the signed-in user to have the `ADMIN`
role and does not require an Email or Support application grant.

The shared staff knowledge search requires an active staff session, an ADMIN/REVIEWER role, Email
or Support application access, and `knowledge.review` on the requested same-organization knowledge
base. Client application names, Referer, and page URLs do not influence authorization.

## Initial administrator status

This change does not provide an executable initial-administrator command. Therefore a completely
empty first deployment cannot yet create its first application administrator through a supported
repository command and must not treat any example command as available.

The next small task is a controlled one-time CLI that reuses invitations and the existing OIDC
first-login path. It must atomically create an organization and `INVITED/ADMIN` user without a
password; identify an existing organization by an immutable ID rather than a possibly duplicated
name; validate the exact verified Google email; and allow first login to bind the stable Google
subject. Repeated execution must not duplicate records, reset identity data, change an existing
role, promote an ordinary user, or reactivate a disabled user. It must add tests for first login,
repeat initialization, email mismatch, and disabled users. It must never run automatically at each
service start and must not expose an unauthenticated public administrator-creation endpoint.

Until that CLI exists, there is no accurate Docker Compose bootstrap command to publish. Grafana's
administrator account is unrelated to application staff identity and must not be used as a
substitute.

## Remaining pages and APIs

- Initial organization/administrator bootstrap CLI: missing and scheduled as the immediate next task.
- Local password creation/recovery pages and APIs: intentionally absent because Google OIDC remains the authentication method.
- Public administrator bootstrap endpoint: intentionally prohibited.
- Dedicated knowledge-assignment editor/API: not added here; the authorization page displays effective knowledge scope from existing `ResourceGrant` data, and this branch does not claim a supported administrator mutation workflow that the repository does not currently expose.
