# Security boundaries

## Authorization and tenancy

Every staff operation starts with an approved OIDC staff session, organization
membership, role, resource grant and (for mutations) CSRF/origin checks. Resource
authorization is evaluated before retrieval, connector, support, email and
operations projections. Missing grants are non-disclosing where the route requires
it. Public chat uses a per-session bearer credential and does not share staff
authority.

## Prompt and content boundary

Retrieved documents, email and chat text are untrusted data, never instructions.
The RAG path applies permission-filtered retrieval, prompt-boundary handling,
groundedness/citation validation, safety classification and refusal on unavailable
or unvalidated answers. Customer responses expose the customer citation projection;
staff assist exposes the staff projection only after staff/resource authorization.
Prompts, chunks and message bodies are excluded from metrics and safe logs.

## Provider and connector boundary

Google Drive uses a read-only scope; Gmail delivery is a separate connector
boundary. Google OIDC, Drive and Gmail client credentials are distinct. Anthropic
and OpenAI keys are provider-specific and may be supplied only by customer-owned
secret management. Provider timeouts/circuit failures fail closed into safe errors;
they do not authorize fallback access to source data.

## Key and data boundary

OAuth tokens are encrypted with envelope encryption. The data-encryption key is
referenced by `GOOGLE_KMS_KEY_NAME`; the key material is not an environment value
or repository artifact. PostgreSQL stores durable workflows, audit metadata and
the erasure ledger. pgBackRest backup encryption uses separate repository secrets.
Restore requires a new generation and erasure-ledger replay before readiness.

## Transport and operations boundary

Nginx enforces TLS, security headers and unbuffered SSE. `/metrics` is classified
as internal in the generated API contract and must be network-restricted by the
customer's ingress policy before the cut line. Alert routing and credential rotation
remain customer-owned operational controls.
