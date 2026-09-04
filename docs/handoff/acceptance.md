# Customer acceptance sign-off

Acceptance is deliberately unsigned until the customer supplies the named owners,
customer-controlled production dependencies and evidence. This document records
the cut-line decision; it does not manufacture customer approval.

## Acceptance gates

- [ ] Customer scope, roles, service hours and acceptance owner are recorded.
- [ ] Asset register has customer owners and receipt for every required asset.
- [ ] Credential rotation is completed and verified for inherited/bootstrap access.
- [ ] Developer access to production assets, secrets, provider accounts and recovery
      systems has been removed or has a customer-approved documented exception.
- [ ] Production deployment, health/readiness, TLS, internal metrics ingress,
      monitoring and alert routing have been demonstrated.
- [ ] PostgreSQL backup/PITR, Redis-loss recovery, Gmail reconciliation and erasure
      replay evidence has been accepted by the recovery/privacy owners.
- [ ] API, architecture, state machines, security boundaries, runbooks and known
      risks have been delivered and administrator training is complete.
- [ ] Release verification and quality evidence are reviewed, with local-fake and
      customer-dependency limitations explicitly recorded.

## Sign-off record

| Decision | Customer acceptance owner | Date | Release tag | Exceptions / residual risks |
|---|---|---|---|---|
| Pending customer acceptance | To be named | To be completed | To be completed | To be completed |

An exception must name a customer risk owner, expiry/review date and approved
mitigation. No exception permits committing or sharing a real credential.
