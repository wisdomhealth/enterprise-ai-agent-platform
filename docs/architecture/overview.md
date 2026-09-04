# Architecture overview

## Runtime map

The platform is a FastAPI API, Celery workers and scheduler, a Next.js staff/public
UI, PostgreSQL 17 with pgvector, Redis, and the production observability stack.
Nginx terminates TLS and forwards API/SSE traffic without buffering. PostgreSQL is
the durable authority; Redis and broker delivery are accelerators, not sources of
business state.

| Module | Durable responsibility | Boundary |
|---|---|---|
| Identity and authorization | Staff sessions, organization/resource grants, audit | Google OIDC and signed server session |
| Connectors and knowledge | Encrypted OAuth connector records, Drive sync, parse jobs, chunks and vectors | Google Drive read-only API |
| RAG | Permission-filtered retrieval, grounded answer and citation projection | Embedding and model providers |
| Public chat and support | Per-session credential, message stream, durable handoff | Browser customer channel and staff queue |
| Email | Gmail intake, draft versions/review, fenced delivery and reconciliation | Gmail API |
| Retention and operations | JobIntent, Outbox, erasure ledger, health, metrics and recovery | PostgreSQL, alerting and runbooks |
| Webhooks | Signed subscription delivery with durable retries | Customer-owned HTTPS receiver |

## Data flow

1. A staff administrator configures an authorized Drive or Gmail connector. OAuth
   material is envelope-encrypted before persistence; keys are referenced through
   `GOOGLE_KMS_KEY_NAME`, never stored in this repository.
2. Connector work is committed as JobIntent and Outbox records. A worker claims a
   PostgreSQL lease before external I/O, publishes safe state, and can be recovered
   from PostgreSQL after Redis or worker loss.
3. Ingestion downloads only authorized Drive content, parses and chunks it, and
   publishes a retrievable version. Retrieval first applies organization/resource
   authorization, then returns a grounded result with source citations.
4. Public chat stores a session credential separately from its identifier. Answers
   pass retrieval, prompt-boundary and groundedness checks before a durable message
   is streamed. Staff assist uses the same authorization-first retrieval but has a
   staff citation projection.
5. Email intake drafts from authorized evidence. A reviewer approves a version
   before delivery. A claimed delivery is either recorded as sent, retried only when
   definitely unsent, or moved to reconciliation when Gmail's outcome is ambiguous.
6. Retention redacts expired content and erasure replay runs after restore before
   readiness reopens. Audit/Outbox records keep identifiers and safe metadata, not
   bodies, OAuth values, prompts, or credentials.

## Operational flow

`/health/live` is process-only. `/health/ready` evaluates the durable dependency,
migration, key-wrapping and restore/erasure gates. `/metrics` exposes aggregates
only. Prometheus, Alertmanager, Loki, Promtail and Grafana make failures visible;
the operating procedure is in [observability](../runbooks/observability.md).

See [state machines](state-machines.md) and [security boundaries](security-boundaries.md)
for the allowed transitions and trust boundaries.
