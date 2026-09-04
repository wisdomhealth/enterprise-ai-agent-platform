# Incident response

## First response

1. Assign an incident commander, preserve timestamps and record the affected
   organization/resource identifiers without copying customer content or secrets.
2. Check `/health/ready`, the safe dashboards and the corresponding alert. Liveness
   alone is not an admission signal.
3. Stop unsafe traffic or automated delivery where required, but do not mutate
   JobIntent leases, audit rows, Outbox rows, provider records or erasure evidence.
4. Follow [the observability runbook](../runbooks/observability.md). This operational
   link corresponds to `docs/runbooks/observability.md` emitted by the operability
   contract. Escalate recovery to [backup recovery](../runbooks/backup-recovery.md)
   and privacy work to [data erasure](../runbooks/data-erasure.md).
5. Close only after the durable state, audit evidence, alert status and customer
   communication have been reviewed by the responsible customer owner.

## Safe error-code playbook

Codes are safe identifiers, not customer content. Capture the code, JobIntent or
delivery/handoff identifier, timestamp, subsystem and correlation/audit identifier.
Do not capture prompts, answers, email bodies, OAuth tokens, headers or secrets.

| Code | Operator action |
|---|---|
| `CHAT_ANSWER_UNAVAILABLE` | Check model and readiness dependencies; keep the customer in the safe refusal path. |
| `CHAT_ANSWER_UNVALIDATED` | Treat as fail closed; inspect groundedness/citation signals and do not manually publish an answer. |
| `CHAT_JOB_INVALID` | Inspect the durable chat job and session authorization; retry only through the approved operation. |
| `CHAT_SAFETY_CLASSIFIER_UNAVAILABLE` | Restore the approved classifier/configuration before reopening affected answer flow. |
| `DELIVERY_RECONCILIATION_REQUIRED` | Reconcile against Gmail history/search; never blind-resend. |
| `DOCUMENT_DOWNLOAD_FORBIDDEN` | Recheck connector/resource authorization and Drive scope; do not broaden scopes. |
| `DOCUMENT_FILE_MISMATCH` | Quarantine the job and verify durable Drive file identity before retry. |
| `DOCUMENT_PARSE_FAILED` | Review the parser-safe failure and source version; retry only if policy permits. |
| `DOCUMENT_PARSE_JOB_UNAVAILABLE` | Check JobIntent/outbox recovery and worker health. |
| `DOCUMENT_PARSE_TRANSIENT_FAILURE` | Use fenced retry after dependency recovery. |
| `DOCUMENT_SOURCE_NOT_FOUND` | Confirm authorized source lifecycle; do not recreate from unverified identifiers. |
| `DRIVE_REAUTH_REQUIRED` | Customer administrator performs the explicit Drive reauthorization flow. |
| `DRIVE_SOURCE_DISABLED` | Review authorized source configuration before enabling it. |
| `DRIVE_SYNC_TRANSIENT_FAILURE` | Check connector health and durable cursor, then retry through the supported action. |
| `EMAIL_APPROVAL_INVALID` | Obtain a current review/approval; never send an invalidated draft. |
| `EMAIL_APPROVAL_INVALIDATED` | Review the new draft version and approve again if appropriate. |
| `EMAIL_CLASSIFICATION_FAILED` | Check provider safe errors and retry only the durable item. |
| `EMAIL_DRAFT_FAILED` | Inspect authorized source/citation availability and retry under review policy. |
| `EMAIL_DRAFT_GROUNDED_ANSWER_UNAVAILABLE` | Restore grounded evidence; do not substitute unsupported text. |
| `EMAIL_DRAFT_PRINCIPAL_SCOPE_MISMATCH` | Correct staff/resource authorization; do not broaden access. |
| `EMAIL_DRAFT_UNAUTHORIZED_CITATION` | Remove unauthorized evidence and regenerate under valid authorization. |
| `EMAIL_JOB_KIND_INVALID` | Stop the job and investigate the durable intent type. |
| `EMAIL_WORKER_TRANSIENT_FAILURE` | Restore worker/dependency health, then use fenced retry. |
| `GMAIL_AUTHORIZATION_FAILED` | Check the Gmail connector identity and explicit reauthorization. |
| `GMAIL_PREVIOUS_ATTEMPT_UNCERTAIN` | Enter reconciliation; do not send a duplicate. |
| `GMAIL_PRE_SEND_FAILURE` | Retry only after confirming no provider-side send occurred. |
| `GMAIL_REAUTH_REQUIRED` | Customer administrator renews the Gmail connector through the approved flow. |
| `GMAIL_RESPONSE_AMBIGUOUS` | Keep `DELIVERY_UNKNOWN` and reconcile provider evidence. |
| `GMAIL_RESPONSE_MISSING_MESSAGE_ID` | Treat as ambiguous delivery and reconcile before any retry. |
| `GMAIL_RESPONSE_MISSING_THREAD_ID` | Treat as ambiguous delivery and reconcile before any retry. |
| `HANDOFF_RESUME_STALE` | Refresh the durable handoff version and require explicit staff action. |
| `INVALID_DOCUMENT_PARSE_JOB` | Inspect the job/source association; do not rebind manually. |
| `JOB_INTENT_INVALID` | Stop and inspect durable intent/audit evidence before recovery. |
| `JOB_NOT_RETRYABLE` | Do not retry; follow the subsystem recovery or remediation procedure. |
| `JOB_VERSION_CONFLICT` | Refresh state and use the caller's versioned action again. |
| `RETENTION_JOB_FAILED` | Preserve the erasure/retention ledger and recover through the fenced job path. |
| `WEBHOOK_DELIVERY_ATTEMPTS_EXHAUSTED` | Contact the customer receiver owner and create a new authorized delivery action. |
| `WEBHOOK_DELIVERY_REJECTED` | Verify receiver signature/response policy without exposing the payload. |
| `WEBHOOK_DELIVERY_RETRYABLE_RESPONSE` | Let the durable retry policy run after receiver remediation. |
| `WEBHOOK_DELIVERY_TRANSPORT_FAILED` | Check network/TLS/receiver status and retain delivery evidence. |
| `WEBHOOK_SUBSCRIPTION_DISABLED` | Confirm customer intent and re-enable only through an authorized administrator action. |

## Severity and communications

- **S0 security/privacy:** contain access, preserve audit evidence, notify the
  customer security/privacy owner, and do not investigate by exporting content.
- **S1 availability/data recovery:** remove traffic when readiness fails; use the
  PITR/erasure replay procedure and record RPO/RTO evidence.
- **S2 workflow/provider:** queue safely, keep grounded-answer and email approval
  gates closed, and coordinate with the connector/provider owner.
- **S3 degraded service:** maintain safe service where readiness is degraded and
  communicate the impact/next update through the customer incident channel.

Post-incident review records timeline, scope, durable evidence, customer actions,
root cause, follow-up owner and whether a known risk/change-control update is needed.
