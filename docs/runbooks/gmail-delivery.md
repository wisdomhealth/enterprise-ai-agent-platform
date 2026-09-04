# Gmail delivery and reconciliation

Every outbound email must come from the current approved draft and its durable
delivery intent. Operators must never send a draft directly in Gmail or bypass
the delivery state machine.

The normal path is `SEND_PENDING -> SENDING -> SENT`. The sender records a
pre-send attempt and uses the delivery intent's deterministic MIME `Message-ID`.
A definitive failure known not to have sent moves to `SEND_RETRY_WAIT`; an
authorized manual retry uses the product retry action and returns the same intent
to `SEND_PENDING`.

`DELIVERY_UNKNOWN` means Gmail may have accepted the message although the local
worker did not receive a conclusive response. Automated and manual send actions
are blocked in this state. An authorized reviewer must use **Reconcile** so the
system searches sent mail by the deterministic Message-ID, approved thread,
recipients, and bounded send window:

- If found, reconciliation records the provider message/thread IDs and moves the
  existing intent to `SENT` without another provider send.
- Only a conclusive absence may be confirmed to return the existing intent to
  `SEND_PENDING`. The subsequent send still uses the normal job, claim, attempt,
  authorization, and idempotency path.

Do not resolve uncertainty by clicking retry, editing database state, creating a
new approval for the same draft, or sending from Gmail. Escalate inconclusive
searches while leaving the intent in `DELIVERY_UNKNOWN`.

Database uniqueness, the deterministic Message-ID, and reconciliation provide
best-effort duplicate-send protection. Gmail is external, so this is not an
exactly-once delivery guarantee.
