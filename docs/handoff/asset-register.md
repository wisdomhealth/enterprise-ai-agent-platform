# Customer asset register

Complete the owner, location/reference, backup/rotation status and receipt for each
row before acceptance. References must identify customer-controlled locations; do
not paste credentials, private keys or token values.

| Asset group | Customer owner | Location/reference | Receipt |
|---|---|---|---|
| Repository and release tags | Customer engineering owner | Customer source-control organization and immutable release tag | Pending customer receipt |
| Container images and deployment scripts | Customer platform operations | Customer registry, image digests and deployment repository | Pending customer receipt |
| VM, DNS, and TLS | Customer infrastructure owner | Customer cloud account, DNS zone and certificate administration | Pending customer receipt |
| PostgreSQL, backups, KMS, and monitoring | Customer data and observability owners | Customer database, backup store, KMS and monitoring tenancy | Pending customer receipt |
| Google Cloud project, Drive/Gmail identities, and OAuth clients | Customer Google Cloud administrator | Customer project and separated identity/client records | Pending customer receipt |
| Claude and OpenAI accounts, budgets, and usage alerts | Customer AI platform owner | Customer provider organizations and budget alerts | Pending customer receipt |
| Data migrations, API documents, state machines, architecture, and security guidance | Customer engineering and security owners | Release tag plus `docs/` handoff package | Pending customer receipt |
| Tests, regression and acceptance sets, evaluations, and cost/latency baselines | Customer quality owner | Customer-controlled test/evaluation storage and release evidence | Pending customer receipt |
| PITR, RPO/RTO, Redis recovery, Gmail reconciliation, and erasure replay evidence | Customer recovery owner | `docs/evidence/` plus customer recovery evidence store | Pending customer receipt |
| Runbooks, incident response, known risks, and administrator training | Customer operations owner | Customer operations knowledge base and this handoff package | Pending customer receipt |

## Transfer controls

Before acceptance, the customer must have administrator control of every listed
asset, a named primary and backup owner, budget/usage alerts for AI providers,
backup restore authority, and an inventory of any retained vendor/developer access.
Credential rotation and removal of developer production access are mandatory
acceptance conditions, not post-handoff cleanup.
