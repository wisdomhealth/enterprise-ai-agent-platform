# Task 17 — Gmail ingestion, classification, and initial draft lifecycle

## Delivered scope

- Applied the ledger ruling with append-only migration `0016_email_ingestion` from
  `0015_support_handoff_lifecycles`. It adds organization-scoped, duplicate-safe email work items,
  state history, durable Gmail sync cursors, and append-only email evaluation runs.
- Added the narrow Gmail read gateway and separately configured connector factory. Both the OAuth
  flow and provider client use exactly `gmail.readonly` and `gmail.send`; employee OIDC tokens are
  not accepted by the connector boundary.
- Added strict Claude classification for the exact category/priority/reply schema. Missing, extra,
  unknown, inconsistent, or coercible boolean fields fail closed, and untrusted email content is
  escaped inside fixed prompt boundaries.
- Added duplicate-safe Gmail message ingestion, normalized message persistence, crash-safe bootstrap
  and history pagination, transactional cursor advancement, safe `REAUTH_REQUIRED` handling, and
  durable classification/draft retry intents.
- Added grounded review-only drafting with the staff RAG audience, organization/knowledge/resource
  citation validation, model/prompt/retrieval/latency/token/cost provenance, state history, audit,
  and Outbox evidence. Task 17 exposes no approval or send transition.
- Registered the Gmail history poll and pending email-job recovery sweep in Celery. History page
  publication can remain in the claimed job transaction, so lease loss rolls back work and cursor
  changes together. Bounded chain keys keep every completed minute poll repeatable without losing
  broker-failure recovery.
- Added fixed regression and held-out acceptance datasets, persistent evaluation-run evidence, the
  credential-free `scripts/run-email-evals` runner, and email triage/evaluation runbooks.

## TDD evidence

- RED/GREEN covered bootstrap pagination retaining its first history anchor, including service-level
  crash-safe resume across pages.
- RED/GREEN covered periodic history jobs stopping after terminal idempotency-key reuse, and provider
  page tokens overflowing the durable job-key column.
- RED/GREEN covered history page/cursor changes deferring commit until job lease completion.
- RED/GREEN covered Outbox persistence errors being masked as model failures instead of rolling back
  the draft transaction.
- RED/GREEN covered prompt-boundary injection and lax boolean coercion at the classifier schema.

## Verification evidence

- Fresh PostgreSQL 17 + pgvector database `platform_task17_20260901` migrated from an empty database
  through `0016_email_ingestion`.
- Migration round-trip `0016 -> 0015 -> 0016` passed; `alembic check` reported no new operations.
- Focused Task 17 unit, Gmail ingestion, drafting, recovery, and durable-task suite passed `25` tests.
- Recovery selection covering cursor rollback, retry, manual retry, and lease-atomic commit passed
  `4` tests (`1` unrelated case deselected).
- Deterministic regression evaluation reported category macro F1 `1.0`, structured-output success
  `1.0`, `96` input tokens, `32` output tokens, and cost `0`; the same model/prompt/dataset evidence
  was read back from `email_evaluation_runs`.
- Scoped Ruff and strict `mypy app` passed for `89` application source files; `git diff --check`
  passed.

## Safety and scope review

- No real Gmail, Google, or Anthropic credential was used. Tests and evaluation use fixed local
  doubles only; the sole external dependency was the authorized disposable local PostgreSQL
  database.
- Raw refresh tokens, provider responses, exception text, and raw MIME content are excluded from
  job, audit, Outbox, and raw-reference payloads.
- The protected pre-existing Task 7 changes in `knowledge/drive_gateway.py` and
  `knowledge/service.py`, and the pre-existing Task 15 report, were not modified, staged, or
  included.
