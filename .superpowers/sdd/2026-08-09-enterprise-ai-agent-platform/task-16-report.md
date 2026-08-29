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
