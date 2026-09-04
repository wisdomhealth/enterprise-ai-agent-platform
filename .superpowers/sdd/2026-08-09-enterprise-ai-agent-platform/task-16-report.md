# Task 16 — Staff support console and Staff Assist interaction

## Scope

- Added the authenticated staff support console, accessible queue, transcript panel,
  explicit Resume AI confirmation, and separate read-only Staff Assist panel.
- Added the public handoff/offline presentation and a Playwright handoff flow.
- Ruling applied: the original Task 15 API exposed only queue metadata, which made
  the approved full staff transcript impossible. Added the smallest read-only
  `GET /api/v1/staff/support/{handoff_id}` detail projection. It reuses existing
  staff, organization, and `knowledge.review` resource authorization and returns
  only durable Handoff/ChatMessage state; it does not alter lifecycle or grants.
- Restored an accidental Node 14 lockfile rewrite. The committed lockfile is
  unchanged; the Playwright `e2e/` location and Vitest project-test include are
  the only verification-configuration changes retained.

## TDD and verification evidence

- RED: `test_staff_can_read_authorized_handoff_transcript_and_context` returned
  `404` before the read-only detail endpoint existed. GREEN: it passed after the
  authorized durable-detail projection was added.
- Focused frontend Task 16 components: `7 passed`.
- Full frontend Vitest suite: `8 files, 11 tests passed` after constraining Vitest
  to project tests (rather than package tests under `node_modules`).
- Local Playwright handoff flow: `1 passed` using the bundled Node 24 runtime and
  a local Next.js server.
- Relevant PostgreSQL support integration suite: `18 passed` against the
  authorized disposable `platform_task15_fix` database.
- Frontend ESLint and TypeScript checks passed. Backend scoped Ruff and `mypy app`
  passed (`79` source files).

## Safety and scope review

- Staff pages verify the server-side OIDC session before rendering; write calls
  retain same-origin credentials and CSRF protection.
- The detail endpoint delegates authorization to `SupportService` and uses
  PostgreSQL-backed transcript state. It does not expose connector credentials,
  provider metadata, or transient worker data.
- The protected Task 7 formatting-only changes in `knowledge/drive_gateway.py`
  and `knowledge/service.py`, and the pre-existing Task 15 report, were not
  modified, staged, or included.

## Fix round 1/5

- A claim conflict now updates only the server-authoritative state and version.
  It preserves the original handoff/session identity and all stable queue data,
  including when another queue item is present.
- Customer answer execution now retains a separate validated staff source
  projection while preserving the existing customer-safe citation projection.
  The staff projection is persisted only on the durable chat-answer Outbox row.
- Staff detail reads accept internal citations only from one authorized,
  `message_id`- and sequence-bound `chat.answer.validated` or
  `chat.answer.refused` event. `SourceCitation` validation fails closed for
  malformed, misbound, duplicate, or non-answer provenance. Public session and
  SSE responses continue to exclude `staff_citations`, chunk/version IDs, and
  internal Drive links.
- The Playwright flow now drives customer handoff, staff claim, staff reply,
  the explicit Resume AI dialog/confirmation, and the resumed customer state.
  Its held SSE response makes the ordering deterministic and verifies that a
  pending stale AI answer is never rendered.

### Fix-round TDD and verification evidence

- RED: production customer answer execution had no independent staff source
  projection; the transcript rendered the wrong field names; and a non-answer
  Outbox event with matching message/sequence was accepted. Each regression
  failed for that specific missing boundary before the implementation changed.
- PostgreSQL 17 + pgvector focused regressions passed against
  `platform_task15_fix` on the existing test container: staff detail/security
  `6`, answer publication `5`, Resume AI `2`, atomic claim `2`, SSE recovery and
  provenance `12`, and job lease fencing `18`.
- RAG answer-service regressions passed `8`; the full frontend Vitest suite
  passed `8 files / 13 tests`; deterministic Playwright passed `1` full handoff
  flow.
- Scoped Ruff passed; `mypy app` passed for `79` source files; frontend ESLint
  and TypeScript checks passed.
- The protected Task 7 formatting-only diffs and the pre-existing Task 15
  report remain unstaged and unchanged by this fix round.

## Fix round 2/5

- `ConversationPanel` now synchronizes its local resource state whenever the
  selected conversation changes, clearing the prior reply draft, notice, and
  confirmation state. A claim conflict on conversation B therefore renders B's
  fetched transcript instead of retaining conversation A's local state. A
  same-conversation version refresh still preserves the just-completed action's
  accessible result notice.
- Queue selection controls are tracked by handoff ID. Both successful and
  conflicting claims restore focus to the control for the operated item rather
  than whichever item rendered last.
- Replaced the request-intercepted Playwright state machine with a test-only
  FastAPI harness backed by the disposable PostgreSQL database. The browser now
  uses the real staff session/CSRF checks and production support router/service
  for queue, claim, conflict, detail, reply, and Resume AI. A seeded pending
  answer job is processed after explicit resume through the real
  `ChatAnswerService` fence; the test asserts no model call, no AI/SYSTEM
  message, no answer Outbox event, and durable `HANDOFF_RESUME_STALE` failure.
- The harness lives only under `backend/tests`, explicitly warns against
  production use, exposes only local test fixture routes, and contains no real
  credentials or external-service mutation.

### Fix-round 2 TDD and verification evidence

- RED: multi-item component regressions reproduced the stale A transcript after
  selecting/conflicting on B and focus landing on the last rendered item before
  the prop synchronization and per-ID focus fixes.
- Full frontend Vitest passed `8 files / 17 tests`; ESLint, TypeScript, and the
  optimized Next.js build passed under the bundled Node 24 runtime.
- The live Playwright lifecycle passed `1` browser test against local FastAPI and
  PostgreSQL, including a real `200` claim followed by stale-version `409`, a
  persisted reply, explicit Resume AI confirmation, and stale-output fencing.
- Independent PostgreSQL files passed: staff detail `6`, answer publication `5`,
  Resume AI `2`, atomic claim `2`, SSE recovery/provenance `12`, job leases `18`,
  RAG answer service `8`, and the new live route/service lifecycle `1`.
- Scoped Ruff passed; `mypy app` passed for `79` source files; strict mypy passed
  for both new test harness files; `git diff --check` passed.
- Protected Task 7 files and the pre-existing Task 15 report remain unstaged and
  excluded from this round.

## Fix round 3/5

- Staff conversation selection now fences asynchronous detail reads with a
  latest-selection token. A delayed detail response for conversation A cannot
  replace conversation B after B is selected through a claim conflict.
- Same-handoff detail synchronization is version-monotonic in both the page and
  `ConversationPanel`. Older detail cannot roll back state or remove a locally
  completed reply/Resume AI result; equal versions merge durable transcript
  messages by sequence.
- The browser harness now executes a fail-closed environment guard before any
  database, FastAPI, or production-app import. It requires the explicit
  `TASK16_E2E=1` sentinel, exact `APP_ENV=test`, a loopback
  `postgresql+asyncpg` URL naming the approved disposable database, and no
  configured provider, Google, Redis, KMS, or connector-file secrets. The
  Playwright server explicitly clears those variables.
- Because the guard prevents the test ASGI module from importing outside the
  approved mode, its `/__e2e__` fixture routes cannot be mounted through this
  harness in development, staging, or production configuration.

### Fix-round 3 TDD and verification evidence

- RED mutation checks: removing the latest-selection token reproduced delayed
  A replacing B; replacing the monotonic panel merge with direct prop assignment
  removed the completed reply and restored Resume AI from the older version.
  Restoring each minimal fix returned the focused frontend tests to green.
- Guard RED rejected the incomplete non-test behavior with expected failures;
  GREEN passed `14` unit/subprocess cases, including proof that unsafe
  sentinel, environment, and provider-secret failures happen before database or
  application initialization.
- Full frontend Vitest passed `8 files / 19 tests`; ESLint, TypeScript, and the
  optimized Next.js build passed under bundled Node `24.19.0`.
- Live PostgreSQL verification passed the route/service lifecycle `1`, staff
  detail/security `6`, answer publication `5`, Resume AI `2`, and atomic claim
  `2` against the approved disposable `platform_task15_fix` database.
- Live Playwright passed `1` complete browser lifecycle with real PostgreSQL,
  real staff session/CSRF enforcement, stale-version `409`, reply persistence,
  explicit Resume AI, and `HANDOFF_RESUME_STALE` output fencing.
- Scoped Ruff and strict mypy for the harness/guard/tests passed; `git diff
  --check` passed. Protected Task 7 files and the pre-existing Task 15 report
  remain unstaged and excluded from this round.

## Fix round 4/5

- Same-handoff equal-version synchronization now unions durable transcript
  messages without overwriting the locally completed reply or Resume AI state.
  A genuinely higher version still replaces the resource in both the staff page
  and `ConversationPanel`.
- Added page- and panel-level regressions for equal-version transcript union,
  completed reply/Resume AI preservation, and higher-version application.
- The E2E guard test now installs an import-order sentinel in a fresh subprocess:
  importing `app.core.database` or `app.main` before the guard is invoked fails
  the test. Every environment variable rejected by the guard is removed from the
  subprocess environment before explicit test values are added.

### Fix-round 4 TDD and verification evidence

- RED exposed equal version 5 reverting a completed Resume AI action back to
  `HUMAN_ACTIVE`. The minimal GREEN change keeps equal-version local state and
  unions messages only.
- Mutation REDs proved that removing equal-version union failed both page and
  panel tests, removing higher-version application failed both consumers, and
  moving the database import ahead of the guard failed the subprocess sentinel.
- Full frontend Vitest passed `8 files / 22 tests`; ESLint, TypeScript, and the
  optimized Next.js build passed under the bundled Node 24 runtime.
- Guard unit/subprocess coverage passed `15`; scoped Ruff and strict mypy passed
  for the guard, harness, and guard tests.
- The live PostgreSQL route/service lifecycle passed `1`; live Playwright passed
  `1` complete claim, stale-version conflict, reply, explicit Resume AI, and
  stale-output fencing lifecycle against the approved disposable database.
- Protected Task 7 files and the pre-existing Task 15 report remain unstaged and
  excluded from this round.
