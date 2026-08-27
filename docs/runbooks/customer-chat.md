# Customer chat operations

## Durable processing and delivery

Customer messages are accepted only with the anonymous session bearer and an
`Idempotency-Key`. The API commits the customer message and a `chat.answer`
job intent together before it asks Celery to wake a worker. A lost broker wake
up is recoverable: the periodic `dispatch_pending_chat_answer_jobs` task scans
the PostgreSQL-backed pending intents and submits them again. Duplicate Celery
deliveries are safe because the worker acquires a database lease and the job
key is bound to one session/message pair.

The worker commits a fully validated customer-safe response before publishing
an optional Redis `chat:sse:<session-id>` hint. Redis contains no authoritative
chat response state. A customer reconnects with the durable SSE event cursor
(`after=<message-sequence>:s:<segment-index>` after a partial answer; legacy
integer message sequences remain accepted). The server reads durable
PostgreSQL events first and then uses Redis only to decide when to read
PostgreSQL again. `message.validated` carries only safe metadata; each
customer-visible sentence is a separately persisted/replayable
`message.segment` event.

## Safe failure behaviour

No provider token is sent to a customer. If generation or validation cannot
produce a `ValidatedAnswer`, the worker stores a generic safe error and a
handoff recommendation, with no provider detail, prompt, source text, token,
or credential. If configuration prevents provider construction, the worker
uses the configured grounded refusal and marks it as a handoff recommendation.

When investigating a delayed response:

1. Locate the `job_intents` row with kind `chat.answer` and the session/message
   reference in its payload.
2. Check the durable job state, attempt count, lease owner, lease expiry, and
   safe error code. Do not infer completion from Redis or Celery alone.
3. Confirm the corresponding `chat_messages` and customer-safe `OutboxEvent`
   exist before expecting an SSE segment.
4. If the job is pending after a broker incident, run the approved pending-job
   dispatcher; do not insert another answer job manually.
5. If the session is no longer `AI_ACTIVE`, leave the worker from generating
   new AI output. Human handoff and explicit Resume AI are managed by the
   support state-machine procedures.

## Security checks

- Public APIs authorize the opaque bearer against the exact session and do not
  expose organization, knowledge-base, Drive, chunk, or staff identifiers.
- SSE emits only persisted validated answer data, customer-safe citations, and
  safe state/error events. It never forwards raw provider output.
- Customer retrieval remains bounded to the session's configured knowledge base
  and uses the existing authorization and retrieval-eligibility filters.
- Do not log bearer values, provider credentials, prompts, retrieved document
  text, or internal source links while troubleshooting.
