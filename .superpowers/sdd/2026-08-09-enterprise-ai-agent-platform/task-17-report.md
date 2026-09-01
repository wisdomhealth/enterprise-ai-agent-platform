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

## Fix round 1/5 — independent-review findings

Production fix commit: `e37d08e`.

### Changes

- Added append-only migration `0017_email_ingestion_hardening` from the published immutable
  `0016_email_ingestion`. It grants the production `platform_app` role the Task 17 table privileges
  required by workers while restricting `email_evaluation_runs` to `SELECT, INSERT`, adds versioned
  complete classification metrics, and distinguishes nonhuman `SYSTEM` state-history actors.
- Added a database-authoritative history-job lease heartbeat using the existing `JobLeaseService`.
  Every execution uses a unique owner, renews only the exact live lease generation, cancels page
  work on lease loss, and retains final completion/retry fencing so cursor/page changes cannot be
  committed by a stale worker.
- Extended evaluation evidence to category, priority, reply-required and exact-match outcomes. The
  informational quality target now uses their aggregate macro F1, so correct categories cannot hide
  incorrect priority or reply decisions.
- Replaced arbitrary employee attribution with a deterministic, purpose- and knowledge-resource-
  bound email worker principal. Automated state history and redacted audit evidence now retain the
  actual `JobIntent` ID and system actor identity. Both PostgreSQL vector and FTS authorization
  branches fail closed for a mismatched resource or purpose.
- Scoped integration assertions to their own organization, connector, message and work item. This
  preserves production-wide polling behavior while keeping cross-session durable test evidence from
  contaminating unrelated assertions.

### Verification

- Fresh authorized PostgreSQL 17 + pgvector database `platform_task17_fix` upgraded from base
  through `0017_email_ingestion_hardening`; the `0017 -> 0016 -> 0017` round trip passed and
  `alembic check` reported no pending operations.
- Real application-role migration verification passed `2` tests: Task 17 worker tables are usable,
  `email_evaluation_runs` permits append/read and rejects update/delete, and the published `0016`
  boundary remains unchanged.
- Real PostgreSQL focused suites passed independently: Gmail ingestion `4`, durable email tasks and
  lease heartbeat `3`, ingestion recovery/provenance `6`, grounded draft provenance `3`, generic job
  leases `18`, and system-principal vector/FTS authorization `1` (`2` unrelated deselected).
- Task 17 unit tests passed `12`. Scoped Ruff passed, strict `mypy app` passed for `90` source files,
  changed-file format checks passed, and `git diff --check` passed.
- The credential-free regression evaluator completed with metrics version
  `email-classification-v2`; persisted readback reported category, priority, reply-required,
  exact-match, aggregate macro F1 and structured-output success all `1.0`. Quality targets remained
  explicitly separate from safety release gates.
- No external Gmail, Google, Anthropic, or OpenAI call was made. The three protected pre-existing
  files retained their exact hashes throughout the repair.
