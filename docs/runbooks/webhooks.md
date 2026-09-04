# Signed webhook operations

The webhook subsystem delivers an explicitly allowlisted projection of durable
PostgreSQL Outbox events. It provides at-least-once delivery. It does not claim
exactly-once delivery and it never uses Redis as delivery authority.

## Configure a subscription

An authenticated administrator creates a subscription through
`POST /api/v1/admin/webhooks/subscriptions` with an `Idempotency-Key`, an HTTPS
endpoint, one or more supported event types, and a signing secret of at least
32 UTF-8 bytes. The secret is returned only by the caller to the API; the API
response never echoes it. PostgreSQL stores only its envelope-encrypted form,
using the same configured KMS or explicitly approved local file-key boundary as
connector credentials.

The endpoint must be HTTPS and must not contain HTTP user information or a URL
fragment. Production egress policy should additionally allow only the approved
n8n origin and deny metadata, loopback, link-local, and unrelated private
networks. Redirects are not followed.

Disabling a subscription is version checked and requires a separate
`Idempotency-Key`. It prevents new delivery attempts. Historical delivery rows
remain as safe operational evidence.

## Consumer verification contract

The sender serializes one canonical UTF-8 JSON body containing only:

- `event_id`
- `event_type`
- `event_version`
- `occurred_at`
- `delivery_attempt`
- `data`, whose keys and scalar value types are allowlisted for the event type

The request includes:

- `X-Webhook-Timestamp`: integer Unix seconds
- `X-Webhook-Signature`: `v1=<lowercase HMAC-SHA256 hex>`
- `Content-Type`: `application/json`

Compute the expected signature over the exact bytes received:

```text
HMAC-SHA256(secret, ascii(timestamp) + "." + exact_request_body)
```

Before processing, the consumer must:

1. Parse the timestamp as integer seconds.
2. Reject it when the absolute difference from its current time exceeds 300
   seconds.
3. Require the `v1=` signature version and compare the digest in constant time.
4. Deduplicate durable processing by `event_id` before applying side effects.

Do not parse and reserialize the JSON before verifying: whitespace or any other
byte change invalidates the signature.

## Safe redelivery and recovery

A legitimate redelivery keeps the original `event_id` and `occurred_at`,
increments `delivery_attempt`, and receives a fresh timestamp and signature.
Consumers therefore reject an old captured request after five minutes while
accepting a later signed attempt and deduplicating its already-processed
`event_id`.

The scheduler records a `ProcessedEvent`, a unique subscription/event delivery
intent, and its `JobIntent` transactionally. Broker wakeups are hints. A periodic
PostgreSQL scan redispatches due pending jobs and expired leases after a worker
or broker restart. Retryable network, timeout, HTTP 408/425/429, and 5xx results
use the bounded `JobIntent` exponential-backoff policy; other HTTP 4xx responses
are terminal.

Only HTTP status plus a SHA-256/byte-count response summary is persisted. Raw
response bodies, exceptions, request bodies, signing secrets, authorization
headers, and internal model/document/email content are never stored as delivery
evidence or logged.

## Incident checks

When deliveries stall, inspect the PostgreSQL `job_intents` row and its related
`webhook_deliveries` row. Correct endpoint or receiver availability first, then
use the approved administrator job retry path. Never manually mark a delivery
successful. Replaying a Celery message is safe because the durable job state,
lease generation, and unique subscription/event identity fence duplicate local
state; the receiver still must deduplicate external side effects by `event_id`.
