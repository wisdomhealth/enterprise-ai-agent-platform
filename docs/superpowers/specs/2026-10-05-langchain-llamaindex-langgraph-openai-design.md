# LangChain, LlamaIndex, LangGraph, and OpenAI Migration Design

## Goal

Replace every active Anthropic generation and classification path with OpenAI through
LangChain, introduce LlamaIndex as the real hybrid-retrieval composition layer, and use
LangGraph to orchestrate grounded-answer execution without weakening the platform's
authorization, durable workflow, approval, or delivery guarantees.

The migration preserves the public API and frontend contracts. It does not rebuild stored
embeddings, replace the existing PostgreSQL schema, create a second answer pipeline, or add
autonomous external actions.

## Framework responsibilities

| Component | Responsibility |
| --- | --- |
| LlamaIndex | Convert authorized `RetrievedChunk` candidates to nodes, compose vector and text retrievers, apply reciprocal-rank fusion and stable deduplication, and host optional reranking/postprocessing. |
| LangChain | Construct OpenAI chat requests, apply trusted prompts, request strict structured outputs, validate Pydantic results, and expose provider response metadata. |
| LangGraph | Execute the bounded answer workflow: scope validation, retrieval, evidence routing, generation, citation validation, retry routing, and safe refusal. |
| Existing application | Authentication, authorization, SQL filtering, document/version state, durable jobs, leases, handoff, approvals, email delivery, Outbox, auditing, persistence, and SSE publication. |

No framework receives authority to approve an email, send external messages, infer tenant
identity, or bypass existing business-state transitions.

## Selected approach

Use adapter-first incremental replacement.

- Preserve the `GenerationProvider`, `GeneratedAnswer`, `Retriever`, `RetrievedChunk`,
  `GroundedAnswerService.answer()`, and `answer_with_evidence()` boundaries where practical.
- Keep the current PostgreSQL vector and full-text query implementations as the only candidate
  sources because they enforce tenant, knowledge-base, resource-grant, active-source,
  retrievable-version, and current-version predicates before ranking.
- Wrap those candidate sources in LlamaIndex retrievers rather than adopting a generic
  LlamaIndex PostgreSQL vector-store schema.
- Keep graph orchestration inside `GroundedAnswerService`; callers in chat, staff assist, and
  email drafting continue using the same service contract.
- Do not add a parallel legacy/new pipeline or feature flag. Tests provide rollback confidence,
  while Git history and the feature branch provide operational rollback.

Rejected alternatives:

1. A framework-centric rewrite of chat, email, jobs, and persistence would move business
   invariants into generic orchestration and create avoidable regression risk.
2. Running old and new RAG pipelines side by side would duplicate embeddings or generation,
   complicate auditing, and violate the single-pipeline requirement.

## Dependencies

The backend remains on Python 3.12/3.13 and pip/setuptools. Add exact direct pins for the three
framework packages rather than introducing a second package manager:

- `langchain-openai==1.6.3`
- `langgraph==1.2.12`
- `llama-index-core==0.14.25`

Remove the Anthropic package after all runtime and test references are migrated. Existing
OpenAI SDK use for embeddings remains behind the current embedding provider.

The selected packages support Python 3.12. Their exact versions are part of the tested
configuration; existing unrelated dependency ranges remain unchanged.

## OpenAI structured-model boundary

### Shared behavior

All active generation and classification paths use LangChain `ChatOpenAI` with:

- an explicitly configured model name;
- a finite request timeout;
- SDK retries disabled so retry ownership remains visible;
- temperature zero where the selected model supports it;
- `with_structured_output(..., include_raw=True)` using explicit Pydantic schemas;
- model identity read from the raw provider response metadata;
- token usage read from raw `AIMessage.usage_metadata`, never from generated content.

Missing usage metadata is distinct from a genuine zero-token value. Existing numeric API and
persistence fields stay compatible, while an explicit completeness/availability flag records
whether the values are authoritative. Known usage from graph retry attempts is accumulated.
If an attempt fails before usage is returned, the totals remain the known lower bound and the
flag is false.

### Grounded answer generation

Replace `AnthropicGenerationProvider` with a LangChain-backed OpenAI provider while preserving
the `GenerationProvider.generate(GroundedPrompt)` contract. The accepted structured payload
contains only answer text and atomic claims with retrieved chunk UUID references. Provider
metadata is attached by application code after schema validation.

Transient transport/status failures retain their existing classification. Invalid or refused
structured results are provider-response failures and can be retried only by the answer graph
within its model-call budget.

### Email and safety classification

Replace `AnthropicEmailClassifier` and `AnthropicStructuredSafetyClassifier` with LangChain
OpenAI implementations. Their existing protocols and strict enums remain authoritative.
Untrusted email or chat text stays delimited and cannot supply organization IDs, permissions,
approval decisions, or tools.

There is no Claude fallback. Missing OpenAI configuration fails closed through the existing
unavailable-provider objects and safe business paths.

## Configuration and cost accounting

Remove Anthropic settings and introduce configurable OpenAI fields with stable defaults:

- generation model;
- classification/safety model;
- embedding model, retaining `text-embedding-3-small` and 1536 dimensions by default;
- provider request timeout;
- whole-answer workflow timeout;
- maximum generation attempts, bounded to a small value and defaulting to two;
- input and output cost per million tokens.

Cost is calculated from configured rates and known aggregate usage. Provider/circuit labels,
readiness dependencies, telemetry, environment examples, Compose configuration, and docs use
`openai`, not `claude`. Historical persisted model names and audit records are not rewritten.

## LlamaIndex retrieval composition

### Authorized branch adapters

The existing vector and PostgreSQL full-text candidate sources remain unchanged. A per-request
LlamaIndex adapter wraps each source and carries trusted principal, knowledge-base ID, result
limit, and the already-computed query embedding. The two branches retain independent SQLAlchemy
sessions and execute concurrently.

Only results already returned by those authorized SQL queries become LlamaIndex nodes. Node
metadata preserves:

- stable chunk ID and UUID;
- document and document-version IDs;
- organization and knowledge-base IDs;
- ordinal, page, section, and title;
- authorization/retrieval eligibility;
- internal Drive link for later staff-only projection.

The original candidate rank is represented as a monotonic score so LlamaIndex does not reorder
a branch before fusion.

### Fusion and conversion

Use LlamaIndex `QueryFusionRetriever` with reciprocal-rank mode, asynchronous execution, and
`num_queries=1`. This disables LLM-generated query variants, prevents an additional model call,
and makes LlamaIndex the actual composition and fusion layer.

Stable node identity and metadata participate in deduplication. Fused nodes are converted back
to the existing `RetrievedChunk` contract before leaving the retriever boundary. Missing,
malformed, cross-tenant, or no-longer-eligible node metadata fails closed instead of producing a
candidate.

Optional application rerankers are adapted as LlamaIndex node postprocessors. Reranking remains
disabled by default and cannot expand the authorized candidate set.

Ingestion, deterministic chunking, stored embeddings, model dimensions, document-version
publication, and production indexes do not change.

## LangGraph answer workflow

### State and runtime context

Graph state contains serializable execution data only: query, retrieved chunks, generation,
validated citations, attempt counts, timing/usage aggregates, outcome, and safe refusal reason.

Trusted runtime context contains the principal, knowledge-base ID, audience, retriever,
generation provider, citation validator, circuit breaker, cost configuration, and telemetry.
Credentials, database sessions, and authority are never copied into graph state. The graph has
no checkpoint database because the existing PostgreSQL `JobIntent` system owns durable retries
and recovery.

### Nodes and routes

```text
scope validation
    -> authorized retrieval
    -> evidence check
       -> no evidence ---------------------------> safe refusal
       -> evidence -> circuit check
          -> open circuit -----------------------> safe refusal
          -> allowed -> OpenAI generation
             -> provider/structure failure
                -> retryable and budget remains -> generation
                -> otherwise --------------------> safe refusal
             -> citation validation
                -> valid ------------------------> validated answer
                -> correctable and budget remains -> generation
                -> otherwise --------------------> safe refusal
```

Authorization and scope failures are never retried. The graph makes at most the configured
number of generation calls. LangChain SDK retries are zero, and the graph does not retry durable
business operations. An outer async timeout bounds the complete answer execution.

The finalization node produces the existing `AnswerExecution`/`ValidatedAnswer` values,
customer/staff citation projection, timing, model, prompt version, known aggregate tokens,
configured cost, and usage completeness.

## Chat, handoff, and SSE guarantees

`ChatAnswerService` remains the durable publication boundary:

1. claim and commit the job lease;
2. classify sensitive content;
3. run the complete answer graph;
4. receive only a validated answer or safe refusal;
5. lock and re-read the conversation state;
6. suppress a late AI answer if human takeover has started;
7. persist message and Outbox event in one transaction;
8. complete the fenced job lease and commit;
9. publish only an ephemeral Redis/SSE hint.

No provider token streaming is added. Public Outbox citations are always reconstructed as
customer-safe citations; staff-only IDs and internal links remain in the restricted projection.
Graph retries occur before persistence and therefore cannot duplicate chat messages or SSE
events.

## Email guarantees

Email drafting continues to call the same grounded-answer service with a service principal and
staff citation projection. The graph performs no email state transition and no delivery.

Existing services continue to own immutable draft versions, reviewer instructions, approval
binding, approval invalidation, deterministic Message-ID generation, delivery leases,
idempotency, known-unsent retries, and ambiguous-delivery reconciliation. Graph retry attempts
cannot create draft versions until one final validated answer returns.

## Error handling

- Missing evidence, open circuit, exhausted correctable retries, or failed citation validation
  returns the configured safe refusal.
- Transient provider failures increment the OpenAI circuit; successful validated generation
  clears it.
- Invalid structured output is never exposed and may consume only the remaining graph retry
  budget.
- Authorization failures and malformed node metadata fail closed without generation.
- Provider, prompt, chunk, credential, and message bodies remain excluded from logs and metrics.
- Existing job/error handling decides whether the surrounding durable task is terminal,
  retryable, or requires handoff/reconciliation.

## Test strategy

Implementation follows red-green-refactor with fake chat models and isolated test databases.

### Unit coverage

- OpenAI structured answer parsing, model metadata, available/unavailable usage, known retry
  usage, transient errors, timeouts, and disabled SDK retries.
- OpenAI email and safety classifiers with strict schema rejection.
- LlamaIndex node round-trip, fusion ordering, stable deduplication, malformed metadata failure,
  and optional postprocessor containment.
- LangGraph success, no-evidence refusal, open-circuit refusal, correctable retry, retry
  exhaustion, authorization failure, and whole-workflow timeout.
- Configurable cost calculation and missing-usage distinction.

### Integration and regression coverage

- Tenant/resource isolation and current-version/source eligibility remain enforced before
  LlamaIndex receives nodes.
- Customer citation projection never exposes chunk IDs, version IDs, or Drive URLs.
- Existing complete-answer-before-SSE behavior and handoff race suppression remain intact.
- Duplicate chat and email jobs remain idempotent.
- Email approval/version checks and delivery idempotency remain unchanged.
- Existing API response and SSE contracts remain compatible.
- Retrieval comparison uses the same fixtures/questions and verifies equivalent RRF ordering.

No test performs paid model calls, modifies production data, or sends a real email.

## Documentation and rollout

Update README, architecture, deployment, readiness, observability, customer-chat, email-triage,
retrieval, and environment/Compose documentation. Document framework ownership, OpenAI setup,
verification commands, known baseline limitations, and rollback.

Rollback is code-only: stop workers/API, restore the prior application revision and its prior
Anthropic configuration, then restart. No database rollback or re-embedding is required because
the migration changes neither schema nor stored vectors. Historical records remain readable.

## Acceptance criteria

- No active Anthropic import, credential requirement, provider construction, health key,
  circuit key, or automatic fallback remains.
- LangChain performs every active OpenAI generation/classification call with validated
  structured output.
- LlamaIndex performs production hybrid retrieval composition and RRF over only authorized
  candidates.
- LangGraph performs the production answer workflow with conditional routing, bounded retries,
  and bounded total time.
- Public APIs, frontend behavior, durable jobs, approvals, email delivery, auditing, Outbox,
  handoff, and SSE guarantees remain compatible.
- Targeted tests, lint, and type checks pass; infrastructure-dependent gaps are reported with
  the exact commands and failure reasons.
