# Task 15 implementation report

## Delivered scope

- Added append-only migration `0014_support_handoffs` from the current published
  `0013_chat_sessions` head. It creates durable handoffs with organization/session
  ownership, trigger, optional sensitive-topic classification, transcript/customer/
  citation/tool snapshot, assignment, handoff boundary and optimistic version.
- Added the explicit handoff state machine. A timeout has no transition;
  `HUMAN_ACTIVE → AI_ACTIVE` only happens through the staff `RESUME_AI` action.
- Added automatic low-confidence/refusal, repeated-failure, sensitive-topic and
  persisted-safe-system-error handoffs, and the bearer-bound public customer-request
  endpoint. Public handoff writes require durable idempotency.
- Added staff queue, atomic claim, reply, resolve and Resume AI APIs. Every staff
  operation is organization and knowledge-resource grant authorized, and replies/
  terminal actions additionally require the assignee or an administrator. Audit and
  Outbox records use only safe identifiers and state metadata.
- Resume AI terminally clears pre-handoff pending/running chat-answer work and waits
  for a later customer message; it cannot release stale AI output.

## TDD evidence

- RED: initial support test collection failed because `app.modules.support` did not
  exist.
- RED: the refusal-to-handoff integration test failed before answer processing
  invoked the durable handoff service.
- GREEN: state-machine, trigger, public request, atomic claim across independent
  PostgreSQL sessions, snapshot/offline contact and Resume AI regressions now pass.

## Fresh verification

Using the existing PostgreSQL 17 + pgvector validation database:

- `alembic downgrade 0013_chat_sessions && alembic upgrade head && alembic check`:
  passed; no model/schema drift.
- `pytest tests/unit/support tests/integration/support -q`: `12 passed`.
- `pytest tests/integration/chat/test_answer_before_stream.py
  tests/integration/chat/test_chat_job_recovery.py
  tests/integration/chat/test_session_access.py -q`: `31 passed`.
- `pytest tests/integration/jobs/test_job_leases.py -q`: `18 passed`.
  This remains a separate process from the chat suite because existing cross-session
  fixtures deliberately dispose the repository's global async engine between
  per-test event loops.
- Scoped Ruff: passed.
- `mypy app`: passed (`79` source files).

## Scope and safety review

- The Task 14 validated-answer/SSE publication boundary remains authoritative; no
  provider token, raw model error, or unvalidated answer is surfaced by this task.
- The two pre-existing Task 7 formatting-only diffs in `knowledge/drive_gateway.py`
  and `knowledge/service.py` were not modified, staged, or included.

## Fix round 1/5

- Explicit Resume AI now closes one handoff lifecycle and permits a later customer
  escalation to create a new durable handoff. Append-only migration
  `0015_support_handoff_lifecycles` removes the one-lifecycle-per-session unique
  constraint without changing published revisions through `0014`.
- Answer publication and every handled failure path re-lock and refresh the
  authoritative PostgreSQL chat-session row. Once human takeover has made the
  session non-`AI_ACTIVE`, stale AI answers, refusals, safety/provider fallbacks,
  SYSTEM messages and their Outbox records fail closed.
- Repeated-failure detection correlates the last two durable AI/SYSTEM messages
  with their persisted refusal/safe-error Outbox provenance rather than guessing
  from visible text. A successful intervening answer resets the consecutive pair.
- Sensitive-topic routing now consumes a typed asynchronous structured-classifier
  boundary; keyword matching is no longer treated as authoritative classification.

### Fix-round TDD and verification evidence

- RED: `test_human_takeover_blocks_stale_system_safe_error_publication` reproduced
  a provider failure after a separately committed `HUMAN_ACTIVE` takeover and
  observed the stale SYSTEM publication before the final failure-boundary refresh.
- GREEN: the exact PostgreSQL regression passed (`1 passed`); the seven support
  test files passed in isolated processes (`3 + 3 + 5 + 2 + 1 + 1 + 2 = 17`
  tests). Isolated processes avoid the repository's known global async-engine /
  per-test-event-loop fixture limitation; a single combined invocation produced
  that fixture error after 16 passing tests, not an application failure.
- Task 14/15 publication and lease regressions passed in isolated processes:
  `test_answer_before_stream.py` (`4 passed`), `test_chat_job_recovery.py`
  (`5 passed`), and `test_job_leases.py` (`18 passed`).
- On authorized disposable PostgreSQL database `platform_task15_fix`, Alembic
  downgrade `0015 -> 0014`, re-upgrade to head, and `alembic check` all passed.
- Scoped Ruff passed; `mypy app` passed for 79 source files.
- The two protected Task 7 formatting-only diffs remained unstaged and unchanged.
