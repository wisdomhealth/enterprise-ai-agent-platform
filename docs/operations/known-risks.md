# Known risks and limits

| Risk or limit | Control and owner |
|---|---|
| Local verification uses deterministic fakes, not customer provider accounts. | Customer AI/Google owner completes preproduction provider acceptance. |
| Model quality targets are informative until customer evaluation data is approved. | Product and customer acceptance owners review evidence; do not claim an SLA. |
| Provider outage, circuit opening or classifier unavailability reduces answer/draft capability. | Fail closed to safe refusal/review and follow observability runbook. |
| Redis or broker loss can delay notifications. | PostgreSQL JobIntent/Outbox state and polling recovery remain authoritative. |
| Gmail timeout can leave send outcome ambiguous. | `DELIVERY_UNKNOWN` requires reconciliation, never a blind resend. |
| Drive/Gmail OAuth access can expire or be revoked. | Customer administrator uses explicit reauthorization with least privilege. |
| `/metrics` is an internal API classification, not an application authentication guarantee. | Customer network owner restricts ingress before the cut line. |
| Backup targets are measured objectives, not an untested recovery SLA. | Data platform performs periodic PITR drills and retains evidence. |
| Retention defaults are not legal advice. | Customer privacy/legal owner approves policy and erasure scope. |
| Container image defaults keep Compose renderable but may not be production backup-ready. | Platform operations pins/scans the approved PostgreSQL pgBackRest image. |

Every accepted residual risk needs a named customer owner, review date and a change
record under [change control](../scope/change-control.md).
