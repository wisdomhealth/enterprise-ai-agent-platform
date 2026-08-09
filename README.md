# Enterprise AI Agent Platform

Executable baseline for an enterprise customer-support platform built with FastAPI and Next.js.

## Quick start

Requirements: Python 3.12 or 3.13, Node.js 20.9+, and Docker Compose.

```bash
cp .env.example .env
make install
make test
make lint
make typecheck
```

The environment example contains empty placeholders only. Supply customer-owned credentials through the environment; do not commit `.env`.

Run the container baseline with `docker compose up --build`. The backend exposes `GET /health/live` on port 8000 and the frontend runs on port 3000. Liveness never connects to PostgreSQL, Redis, or external APIs.

See [the platform baseline runbook](docs/runbooks/platform-baseline.md) for operating commands and [the readiness checklist](docs/readiness/checklist.md) for the explicit not-ready delivery gates.
