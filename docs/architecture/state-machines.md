# Approved state machines

Only the transitions below are valid. Durable actions are audited and use the
current version/lease where the implementation requires it.

## Customer chat and staff handoff

`AI_ACTIVE` → `HANDOFF_REQUESTED` → `QUEUED` → `HUMAN_ACTIVE`

- `HUMAN_ACTIVE` → `HUMAN_ACTIVE` for a staff reply.
- `HUMAN_ACTIVE` → `RESOLVED` only through an explicit staff resolution.
- `HUMAN_ACTIVE` → `AI_ACTIVE` only through an explicit Resume AI action.
- Handoff timeout does not silently return the customer to AI; it leaves the
  durable workflow for staff handling.

## Email drafting, review, delivery and reconciliation

`INGESTED` → `DRAFTING` → `AWAITING_REVIEW` → `APPROVED` → `SEND_PENDING`
→ `SENDING` → `SENT`

- Classification or drafting failure goes to `DRAFT_RETRY_WAIT`; an approved
  retry returns to `DRAFTING`.
- A reviewer may regenerate from `AWAITING_REVIEW`; an explicit rejection enters
  `REJECTED`.
- A draft edit or regeneration invalidates a previous approval and returns the
  item to `AWAITING_REVIEW`.
- A known-unsent delivery failure goes from `SENDING` to `SEND_RETRY_WAIT`, then
  to `SEND_PENDING` through a controlled retry.
- An ambiguous Gmail response goes from `SENDING` to `DELIVERY_UNKNOWN`. Reconcile
  to `SENT` only when a provider-side send is found; otherwise return to
  `SEND_PENDING`. Never blindly resend from `DELIVERY_UNKNOWN`.
- An unrecoverable failure or exhausted retry budget enters `FAILED_TERMINAL`.

The delivery worker's JobIntent lease and delivery attempt record fence external
I/O. PostgreSQL remains the reconciliation/recovery authority.
