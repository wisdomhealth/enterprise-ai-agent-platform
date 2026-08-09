# Platform baseline runbook

## Prerequisites

- Python 3.12 or 3.13
- Node.js 20.9 or newer
- Docker with Compose

## Local setup

Copy `.env.example` to `.env` and populate only customer-provided values. The example intentionally contains no credentials. Then run:

```bash
make install
make test
make lint
make typecheck
```

## Containers

Validate definitions without credentials:

```bash
docker compose config
docker compose -f compose.test.yaml config
```

Start the baseline with `docker compose up --build`. The backend liveness probe is `GET /health/live`; it intentionally does not contact PostgreSQL, Redis, or external APIs.

## Readiness

The executable baseline is not production readiness. Track every customer-owned prerequisite and its evidence in `docs/readiness/checklist.md`; all gates remain not ready until evidence is supplied and approved.
