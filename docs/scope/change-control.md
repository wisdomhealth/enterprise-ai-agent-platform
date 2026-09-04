# Scope freeze and change control

## Scope freeze

The scope freeze is the approved first-release behavior represented by the
implemented API contract, state machines, security boundaries, Compose runtime,
runbooks and acceptance gates. Changes are not accepted merely because they are
small, urgent or supported by a provider capability.

## Cut line

The cut line is the immutable release tag selected after documentation checks and
release verification pass, but before customer acceptance is signed. Customer-owned
credentials, DNS/TLS, provider and production-account checks remain external
readiness inputs and are never invented or committed to cross the cut line.

## Change request

Each requested change records: requester, customer owner, business reason,
in-scope/out-of-scope decision, API/schema/state-machine impact, authorization and
privacy impact, migration/rollback plan, provider/cost impact, test/evidence plan,
runbook/training change, release target and acceptance decision.

Changes affecting identity, resource authorization, connector scope, prompt safety,
citations, delivery, retention/erasure, backup/recovery, production ingress or
credentials require security/privacy and operations review. Publish a new tag and
repeat applicable verification; never amend an accepted release tag or migration.
