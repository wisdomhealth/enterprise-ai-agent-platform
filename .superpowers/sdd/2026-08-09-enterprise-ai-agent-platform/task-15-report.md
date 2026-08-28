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
