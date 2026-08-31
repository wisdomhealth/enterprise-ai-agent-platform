# Email classification evaluation

The repository contains a fixed development regression set and a held-out acceptance set:

- `backend/tests/fixtures/email-evals/regression.jsonl`
- `backend/tests/fixtures/email-evals/acceptance.jsonl`

Every row contains a sanitized message ID, subject, body, expected category, expected priority, and
expected reply-required decision. Dataset kind and version are repeated per row and validated as one
consistent immutable input. Acceptance labels must not be used for tuning.

## Local deterministic run

From the repository root:

```bash
scripts/run-email-evals \
  --dataset backend/tests/fixtures/email-evals/regression.jsonl \
  --provider fake \
  --no-persist
```

The fake provider is deterministic and uses no external credentials. The JSON report records the
dataset version and SHA-256 digest, model and prompt version, category macro F1, structured-output
success rate, total latency, input/output tokens, and estimated cost.

To append the same run evidence to `email_evaluation_runs`, point `DATABASE_URL` at an already
migrated, isolated PostgreSQL database and omit `--no-persist`. Never point a local run at production.

```bash
DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/platform_task17_verify \
scripts/run-email-evals \
  --dataset backend/tests/fixtures/email-evals/regression.jsonl \
  --provider fake
```

## Interpreting results

The informational quality targets are:

- category macro F1: at least `0.85`
- strict structured-output success: at least `0.99`

The runner prints these targets separately from safety release gates. Passing the classification
targets does not authorize release, Gmail sending, or use of the held-out acceptance set for prompt
tuning. A release must also pass authorization, citation, duplicate-ingestion, cursor rollback,
retry/recovery, migration, and provider fault-injection gates.

Use the regression set during development. Run the held-out acceptance set only for milestone or
release acceptance, retain its digest and persisted run ID with the release evidence, and do not
change a dataset without assigning a new dataset version.
