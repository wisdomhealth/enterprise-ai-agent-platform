# LangChain, LlamaIndex, LangGraph, and OpenAI Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace active Anthropic paths with LangChain OpenAI, make LlamaIndex own authorized hybrid retrieval composition, and make LangGraph own bounded grounded-answer orchestration without changing public APIs or durable business workflows.

**Architecture:** Existing PostgreSQL candidate queries remain the authorization boundary. LlamaIndex wraps and fuses only their authorized candidates, LangChain performs strict OpenAI structured calls, and a non-checkpointed LangGraph runs inside `GroundedAnswerService`; chat, jobs, handoff, email approval/delivery, Outbox, and persistence stay in existing services.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy async, LangChain OpenAI 1.6.3, LangGraph 1.2.12, LlamaIndex Core 0.14.25, OpenAI, pytest, Ruff, mypy.

---

## File map

- Create `backend/app/core/openai.py`: shared LangChain structured-output invocation, trusted model metadata, and usage extraction.
- Modify `backend/app/core/config.py`: OpenAI model, timeout, graph-budget, embedding-model, and cost settings; remove Anthropic settings.
- Modify `backend/app/core/telemetry.py`: record known usage without treating missing metadata as zero.
- Modify `backend/app/modules/rag/llm.py`: OpenAI `GenerationProvider`, provider errors, and usage completeness.
- Modify `backend/app/modules/rag/prompts.py`: retry-specific trusted generation guidance and new prompt version.
- Modify `backend/app/modules/rag/retriever.py`: LlamaIndex branch adapters, node conversion, RRF composition, and optional node postprocessing.
- Create `backend/app/modules/rag/workflow.py`: bounded LangGraph answer workflow with runtime context and no checkpoint store.
- Modify `backend/app/modules/rag/answer_service.py`: compatible service facade over the graph and configurable cost/timeout behavior.
- Modify `backend/app/modules/rag/types.py`: backward-compatible usage completeness on validated answers.
- Modify `backend/app/modules/email/classification.py`: LangChain OpenAI classifier.
- Modify `backend/app/modules/email/schemas.py`: usage completeness in draft provenance.
- Modify `backend/app/modules/email/tasks.py`: OpenAI classifier and grounded-service wiring.
- Modify `backend/app/modules/email/drafting.py`: propagate usage completeness.
- Modify `backend/app/modules/support/triggers.py`: LangChain OpenAI safety classifier.
- Modify `backend/app/modules/chat/tasks.py`: OpenAI safety-classifier construction.
- Modify `backend/app/main.py` and `backend/app/modules/operations/health.py`: OpenAI-only startup/readiness wiring.
- Modify dependency, environment, Compose, README, architecture, deployment, readiness, and runbook files to remove active Claude requirements.
- Replace Anthropic-specific unit tests and extend RAG/chat/email regression coverage.

### Task 1: Pin framework dependencies and define OpenAI configuration

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`
- Test: `backend/tests/unit/core/test_openai_config.py`

- [ ] **Step 1: Write failing configuration tests**

```python
from app.core.config import Settings


def test_openai_models_costs_and_budgets_have_stable_defaults() -> None:
    settings = Settings()
    assert settings.openai_generation_model == "gpt-4.1-mini"
    assert settings.openai_classifier_model == "gpt-4.1-mini"
    assert settings.openai_embedding_model == "text-embedding-3-small"
    assert settings.openai_request_timeout_seconds == 30.0
    assert settings.rag_execution_timeout_seconds == 60.0
    assert settings.rag_max_generation_attempts == 2
    assert settings.openai_input_cost_per_million >= 0
    assert settings.openai_output_cost_per_million >= 0


def test_anthropic_is_not_a_runtime_setting() -> None:
    assert "anthropic_api_key" not in Settings.model_fields
    assert "anthropic_model" not in Settings.model_fields
```

- [ ] **Step 2: Run the test and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/core/test_openai_config.py -q`

Expected: FAIL because the new OpenAI settings do not exist and Anthropic settings still do.

- [ ] **Step 3: Add exact framework pins and minimal settings**

Add to `backend/pyproject.toml`:

```toml
"langchain-openai==1.6.3",
"langgraph==1.2.12",
"llama-index-core==0.14.25",
```

Remove `anthropic`. Define strict bounded settings in `Settings`:

```python
openai_generation_model: str = Field(
    default="gpt-4.1-mini", validation_alias="OPENAI_GENERATION_MODEL"
)
openai_classifier_model: str = Field(
    default="gpt-4.1-mini", validation_alias="OPENAI_CLASSIFIER_MODEL"
)
openai_embedding_model: str = Field(
    default="text-embedding-3-small", validation_alias="OPENAI_EMBEDDING_MODEL"
)
openai_request_timeout_seconds: float = Field(
    default=30.0, gt=0, le=120, validation_alias="OPENAI_REQUEST_TIMEOUT_SECONDS"
)
rag_execution_timeout_seconds: float = Field(
    default=60.0, gt=0, le=300, validation_alias="RAG_EXECUTION_TIMEOUT_SECONDS"
)
rag_max_generation_attempts: int = Field(
    default=2, ge=1, le=3, validation_alias="RAG_MAX_GENERATION_ATTEMPTS"
)
openai_input_cost_per_million: float = Field(
    default=0.0, ge=0, validation_alias="OPENAI_INPUT_COST_PER_MILLION"
)
openai_output_cost_per_million: float = Field(
    default=0.0, ge=0, validation_alias="OPENAI_OUTPUT_COST_PER_MILLION"
)
```

Keep `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `SAFETY_CLASSIFIER_MODEL`; remove all Anthropic fields and example variables.

- [ ] **Step 4: Install and verify GREEN**

Run: `backend/.venv/bin/python -m pip install -e './backend[dev]'`

Run: `cd backend && .venv/bin/python -m pytest tests/unit/core/test_openai_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/app/core/config.py backend/tests/unit/core/test_openai_config.py .env.example
git commit -m "build: pin AI frameworks and OpenAI configuration"
```

### Task 2: Build the shared LangChain structured-output boundary

**Files:**
- Create: `backend/app/core/openai.py`
- Test: `backend/tests/unit/core/test_openai_structured.py`

- [ ] **Step 1: Write failing tests for trusted metadata and missing usage**

Use a fake `with_structured_output` runnable returning `{"raw": AIMessage, "parsed": schema, "parsing_error": None}`. Cover:

```python
@pytest.mark.asyncio
async def test_structured_client_reads_model_and_usage_from_raw_message() -> None:
    raw = AIMessage(
        content="",
        response_metadata={"model_name": "gpt-test"},
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    result = await client_returning(raw, AnswerPayload(text="Answer", claims=[])).invoke(
        AnswerPayload, system="rules", user="question"
    )
    assert result.model == "gpt-test"
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.complete is True


@pytest.mark.asyncio
async def test_structured_client_distinguishes_missing_usage_from_zero() -> None:
    result = await client_returning(
        AIMessage(content="", response_metadata={"model_name": "gpt-test"}),
        AnswerPayload(text="Answer", claims=[]),
    ).invoke(AnswerPayload, system="rules", user="question")
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.complete is False
```

Also test missing model metadata and `parsing_error` raise `OpenAIStructuredResponseError` without exposing provider output.

- [ ] **Step 2: Run and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/core/test_openai_structured.py -q`

Expected: FAIL because `app.core.openai` does not exist.

- [ ] **Step 3: Implement the minimal shared client**

Implement immutable result types and one invocation class:

```python
@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    complete: bool


@dataclass(frozen=True, slots=True)
class StructuredModelResult(Generic[SchemaT]):
    value: SchemaT
    model: str
    usage: ModelUsage


class LangChainStructuredClient:
    def __init__(self, model: object) -> None:
        self._model = model

    async def invoke(
        self,
        schema: type[SchemaT],
        *,
        system: str,
        user: str,
    ) -> StructuredModelResult[SchemaT]:
        runnable = self._model.with_structured_output(
            schema, method="json_schema", include_raw=True, strict=True
        )
        response = await runnable.ainvoke([SystemMessage(system), HumanMessage(user)])
        return _validated_result(response, schema)
```

Construct live `ChatOpenAI` through one factory with `timeout=settings.openai_request_timeout_seconds`, `max_retries=0`, configured API key/base URL/model, and no tool binding.

- [ ] **Step 4: Verify GREEN and static checks**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/core/test_openai_structured.py -q`

Run: `cd backend && .venv/bin/python -m ruff check app/core/openai.py tests/unit/core/test_openai_structured.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/openai.py backend/tests/unit/core/test_openai_structured.py
git commit -m "feat: add LangChain structured OpenAI boundary"
```

### Task 3: Replace grounded Claude generation with LangChain OpenAI

**Files:**
- Modify: `backend/app/modules/rag/llm.py`
- Modify: `backend/app/modules/rag/types.py`
- Replace: `backend/tests/unit/rag/test_anthropic_provider.py` with `backend/tests/unit/rag/test_openai_provider.py`
- Modify: `backend/tests/unit/rag/test_provider_circuit.py`

- [ ] **Step 1: Write failing OpenAI provider tests**

Test the preserved provider contract:

```python
@pytest.mark.asyncio
async def test_openai_provider_attaches_only_trusted_response_metadata() -> None:
    provider = OpenAIGenerationProvider(fake_structured_client(
        value=_StructuredGeneration(text="Refunds take five business days.", claims=[]),
        model="gpt-test",
        usage=ModelUsage(11, 7, True),
    ))
    answer = await provider.generate(build_grounded_prompt("When?", []))
    assert answer.model == "gpt-test"
    assert answer.input_tokens == 11
    assert answer.output_tokens == 7
    assert answer.usage_complete is True
```

Test transient timeouts become `ProviderTransientError`, malformed structured results become `ProviderResponseError`, and provider errors retain known usage when available.

- [ ] **Step 2: Run and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/rag/test_openai_provider.py tests/unit/rag/test_provider_circuit.py -q`

Expected: FAIL because `OpenAIGenerationProvider` and `usage_complete` do not exist.

- [ ] **Step 3: Implement the OpenAI adapter and remove Anthropic code**

Keep `_StructuredGeneration` as the strict Pydantic payload and add:

```python
class OpenAIGenerationProvider:
    def __init__(self, client: LangChainStructuredClient) -> None:
        self._client = client

    async def generate(self, prompt: GroundedPrompt) -> GeneratedAnswer:
        try:
            result = await self._client.invoke(
                _StructuredGeneration,
                system=prompt.system_message,
                user=prompt.user_message,
            )
        except OpenAIStructuredResponseError as error:
            raise ProviderResponseError("OpenAI returned invalid structured answer data", error.usage) from error
        except Exception as error:
            if _is_transient_provider_error(error):
                raise ProviderTransientError("OpenAI temporarily unavailable") from error
            raise
        return GeneratedAnswer(
            text=result.value.text,
            claims=result.value.claims,
            model=result.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            usage_complete=result.usage.complete,
        )
```

Add `usage_complete: bool = True` to `GeneratedAnswer` and `ValidatedAnswer`. Rename circuit tests from `claude` to `openai`. Remove Anthropic imports/classes/helpers.

- [ ] **Step 4: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/rag/test_openai_provider.py tests/unit/rag/test_provider_circuit.py tests/unit/rag/test_groundedness.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/rag/llm.py backend/app/modules/rag/types.py backend/tests/unit/rag/test_openai_provider.py backend/tests/unit/rag/test_provider_circuit.py
git rm backend/tests/unit/rag/test_anthropic_provider.py
git commit -m "feat: replace Claude answer generation with LangChain OpenAI"
```

### Task 4: Replace email and safety classifiers with LangChain OpenAI

**Files:**
- Modify: `backend/app/modules/email/classification.py`
- Modify: `backend/app/modules/support/triggers.py`
- Modify: `backend/tests/unit/email/test_classification_schema.py`
- Modify: `backend/tests/unit/support/test_triggers.py`

- [ ] **Step 1: Write failing classifier tests**

Rename Claude tests and verify exact Pydantic schema enforcement, prompt delimiters, trusted model/usage metadata, and safe failure classes:

```python
@pytest.mark.asyncio
async def test_openai_email_classifier_uses_structured_result_metadata() -> None:
    classifier = OpenAIEmailClassifier(fake_client(
        EmailClassification(category="UNKNOWN", priority="NORMAL", reply_required=True),
        model="gpt-classifier",
        usage=ModelUsage(11, 5, True),
    ))
    execution = await classifier.classify("Subject", "Body")
    assert execution.model == "gpt-classifier"
    assert execution.usage_complete is True


@pytest.mark.asyncio
async def test_openai_safety_classifier_accepts_only_sensitive_topic_schema() -> None:
    classifier = OpenAIStructuredSafetyClassifier(fake_client(
        SensitiveTopicClassification(sensitive_topic="PRIVACY_REQUEST")
    ))
    result = await classifier.classify("Delete my data")
    assert result.sensitive_topic is SensitiveTopic.PRIVACY_REQUEST
```

- [ ] **Step 2: Run and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/email/test_classification_schema.py tests/unit/support/test_triggers.py -q`

Expected: FAIL because OpenAI classifier classes do not exist.

- [ ] **Step 3: Implement minimal OpenAI classifiers**

Use `LangChainStructuredClient` and the existing schemas. Preserve `EmailClassifier`, `StructuredSafetyClassifier`, response-error, and unavailable-error protocols/classes. Add `usage_complete: bool = True` to `ClassificationExecution`. Escape/delimit untrusted content exactly as before. Delete all Anthropic imports and constructors.

- [ ] **Step 4: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/email/test_classification_schema.py tests/unit/support/test_triggers.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/email/classification.py backend/app/modules/support/triggers.py backend/tests/unit/email/test_classification_schema.py backend/tests/unit/support/test_triggers.py
git commit -m "feat: migrate classifiers to LangChain OpenAI"
```

### Task 5: Make LlamaIndex own hybrid retrieval composition

**Files:**
- Modify: `backend/app/modules/rag/retriever.py`
- Modify: `backend/tests/integration/rag/test_retriever.py`
- Modify: `backend/tests/integration/rag/test_hybrid_permissions.py`

- [ ] **Step 1: Write failing node/fusion tests**

Extend the existing fake-branch test to assert LlamaIndex output and metadata round-trip:

```python
@pytest.mark.asyncio
async def test_llamaindex_fuses_authorized_branches_and_deduplicates_stable_chunks() -> None:
    result = await HybridRetriever(VectorSource([chunk("a"), chunk("b")]),
                                   TextSource([chunk("b"), chunk("c")]),
                                   Provider()).retrieve(principal, knowledge_base_id, "policy", 10)
    assert [item.stable_id for item in result] == ["b", "a", "c"]
    assert result[0].document_version_id == original_b.document_version_id
    assert result[0].page_number == original_b.page_number
```

Add tests that malformed/cross-tenant node metadata fails closed, both branches overlap on distinct sessions, `num_queries=1` causes no LLM query generation, and an optional node postprocessor cannot add an unauthorized node.

- [ ] **Step 2: Run and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/integration/rag/test_retriever.py -k 'llamaindex or hybrid_retriever_runs' -q`

Expected: FAIL because the retriever still calls the local `reciprocal_rank_fusion` function.

- [ ] **Step 3: Implement LlamaIndex branch adapters and fusion**

In `retriever.py`:

```python
class _AuthorizedBranchRetriever(BaseRetriever):
    def __init__(self, search: Callable[[str], Awaitable[list[RetrievedChunk]]], limit: int) -> None:
        self._search = search
        self._limit = limit
        super().__init__()

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        chunks = await self._search(query_bundle.query_str)
        return [
            NodeWithScore(node=_chunk_to_node(chunk), score=float(self._limit - rank))
            for rank, chunk in enumerate(chunks)
        ]

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        raise RuntimeError("authorized retrieval is async-only")
```

Create vector/text instances per call, pass them to `QueryFusionRetriever` with `FUSION_MODES.RECIPROCAL_RANK`, `num_queries=1`, `use_async=True`, and `similarity_top_k=limit`. Convert fused nodes back with strict UUID/bool/int/string metadata validation and recheck principal/knowledge-base scope. Apply optional LlamaIndex node postprocessors only to the already-authorized fused list and reject newly introduced node IDs.

Keep the single query embedding calculation and independent session guard.

- [ ] **Step 4: Verify unit-style and PostgreSQL retrieval tests**

Run: `cd backend && .venv/bin/python -m pytest tests/integration/rag/test_retriever.py -k 'hybrid_retriever_runs or llamaindex' -q`

When PostgreSQL is available, run:

`cd backend && .venv/bin/python -m pytest tests/integration/rag/test_retriever.py tests/integration/rag/test_hybrid_permissions.py -q`

Expected: RRF ordering remains `b, a, c`; database tests preserve tenant isolation and document eligibility.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/rag/retriever.py backend/tests/integration/rag/test_retriever.py backend/tests/integration/rag/test_hybrid_permissions.py
git commit -m "feat: compose authorized hybrid retrieval with LlamaIndex"
```

### Task 6: Introduce the bounded LangGraph answer workflow

**Files:**
- Create: `backend/app/modules/rag/workflow.py`
- Modify: `backend/app/modules/rag/prompts.py`
- Test: `backend/tests/unit/rag/test_answer_workflow.py`

- [ ] **Step 1: Write failing graph-routing tests**

Use fake retrievers/providers/circuit stores. Cover:

```python
@pytest.mark.asyncio
async def test_graph_generates_once_and_validates_supported_answer() -> None:
    execution = await workflow(valid_generation).run(request)
    assert execution.answer.refused is False
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_graph_refuses_without_calling_model_when_evidence_is_empty() -> None:
    execution = await workflow_with_chunks([]).run(request)
    assert execution.answer.refused is True
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_graph_retries_correctable_validation_once_then_stops() -> None:
    provider.answers = [unsupported_generation, supported_generation]
    execution = await workflow(provider, max_attempts=2).run(request)
    assert execution.answer.refused is False
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_graph_does_not_retry_authorization_failure() -> None:
    execution = await workflow(raising_retriever(PermissionError())).run(request)
    assert execution.answer.refused is True
    assert provider.calls == 0
```

Also cover transient retry exhaustion, circuit-open refusal, known retry usage aggregation, incomplete usage propagation, and total timeout.

- [ ] **Step 2: Run and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/rag/test_answer_workflow.py -q`

Expected: FAIL because `rag.workflow` does not exist.

- [ ] **Step 3: Implement graph state, trusted context, and routes**

Use `StateGraph(AnswerState, context_schema=AnswerRuntimeContext)` without a checkpointer. Define nodes:

```python
builder.add_node("validate_scope", validate_scope)
builder.add_node("retrieve", retrieve)
builder.add_node("check_circuit", check_circuit)
builder.add_node("generate", generate)
builder.add_node("validate_citations", validate_citations)
builder.add_node("validated", validated)
builder.add_node("refuse", refuse)
builder.add_edge(START, "validate_scope")
builder.add_conditional_edges("validate_scope", route_scope)
builder.add_conditional_edges("retrieve", route_evidence)
builder.add_conditional_edges("check_circuit", route_circuit)
builder.add_conditional_edges("generate", route_generation)
builder.add_conditional_edges("validate_citations", route_validation)
builder.add_edge("validated", END)
builder.add_edge("refuse", END)
graph = builder.compile()
```

Runtime context holds principal/services and immutable configuration; graph state holds only request/result data. Wrap `graph.ainvoke` in `asyncio.timeout(total_timeout)`. Increment generation attempts before each call. Retry only transient/structured/citation failures while `attempts < max_attempts`; never retry scope/authorization/no-evidence paths.

Update `build_grounded_prompt(query: str, chunks: list[RetrievedChunk], *, retry_instruction: bool = False)` so retries add only a fixed trusted instruction and bump `PROMPT_VERSION` to `grounded-answer-v2`.

- [ ] **Step 4: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/rag/test_answer_workflow.py tests/unit/rag/test_prompt_boundaries.py tests/unit/rag/test_groundedness.py -q`

Expected: PASS with maximum provider calls proven by assertions.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/rag/workflow.py backend/app/modules/rag/prompts.py backend/tests/unit/rag/test_answer_workflow.py backend/tests/unit/rag/test_prompt_boundaries.py
git commit -m "feat: orchestrate grounded answers with LangGraph"
```

### Task 7: Preserve the GroundedAnswerService facade and configurable accounting

**Files:**
- Modify: `backend/app/modules/rag/answer_service.py`
- Modify: `backend/app/core/telemetry.py`
- Modify: `backend/app/modules/email/schemas.py`
- Modify: `backend/app/modules/email/drafting.py`
- Modify: `backend/tests/integration/rag/test_answer_service.py`
- Modify: `backend/tests/integration/rag/test_evaluation_provenance.py`
- Modify: `backend/tests/integration/email/test_draft_generation.py`

- [ ] **Step 1: Write failing compatibility/accounting tests**

Keep all current answer/citation tests and add:

```python
def test_cost_uses_configured_rates() -> None:
    assert estimated_cost(1_000_000, 2_000_000, input_rate=1.25, output_rate=5.0) == 11.25


@pytest.mark.asyncio
async def test_retry_usage_and_completeness_reach_answer_and_email_provenance() -> None:
    principal, knowledge_base_id = answer_scope()
    execution = await service_with_two_attempts().answer_with_evidence(
        principal,
        knowledge_base_id,
        "How long do refunds take?",
        AnswerAudience.STAFF,
    )
    assert execution.answer.input_tokens == 20
    assert execution.answer.output_tokens == 12
    assert execution.answer.usage_complete is False
```

Verify telemetry increments token/cost counters only for known values while recording a separate missing-usage count/outcome.

- [ ] **Step 2: Run and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/integration/rag/test_answer_service.py tests/integration/email/test_draft_generation.py -q`

Expected: FAIL because service orchestration/costs are still Claude-specific and provenance lacks completeness.

- [ ] **Step 3: Refactor the service facade**

`GroundedAnswerService.answer_with_evidence()` delegates to the compiled workflow and preserves its return type. `from_settings()` constructs:

- configured OpenAI embedding provider;
- LlamaIndex-backed `HybridRetriever`;
- `OpenAIGenerationProvider` over `ChatOpenAI`;
- existing citation validator and Redis circuit breaker;
- graph limits/timeouts and configured cost rates.

Use circuit key `openai`. Add `usage_complete` to `EmailDraftProvenance` with a compatibility default and propagate it. Replace fixed `$3/$15` calculation with configured rates. Never label a refusal as model usage; mark it incomplete/absent while preserving numeric response compatibility.

- [ ] **Step 4: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/integration/rag/test_answer_service.py tests/integration/rag/test_evaluation_provenance.py tests/integration/email/test_draft_generation.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/rag/answer_service.py backend/app/core/telemetry.py backend/app/modules/email/schemas.py backend/app/modules/email/drafting.py backend/tests/integration/rag/test_answer_service.py backend/tests/integration/rag/test_evaluation_provenance.py backend/tests/integration/email/test_draft_generation.py
git commit -m "refactor: preserve answer service over bounded graph"
```

### Task 8: Wire OpenAI through chat, email, startup, and readiness

**Files:**
- Modify: `backend/app/modules/chat/tasks.py`
- Modify: `backend/app/modules/email/tasks.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/modules/operations/health.py`
- Modify: `backend/tests/unit/core/test_celery_database.py`
- Modify: `backend/tests/integration/operations/test_readiness.py`
- Modify: `backend/tests/integration/chat/test_answer_before_stream.py`
- Modify: `backend/tests/integration/chat/test_chat_job_recovery.py`
- Modify: `backend/tests/integration/support/test_answer_triggers.py`
- Modify: `backend/tests/integration/email/test_tasks.py`
- Modify: `backend/tests/integration/email/test_review_conflicts.py`
- Modify: `backend/tests/integration/email/test_delivery_intent.py`

- [ ] **Step 1: Write failing wiring/regression tests**

Assert app startup needs OpenAI+Redis only, readiness exposes `openai` not `claude`, workers construct OpenAI classifiers, and existing behavioral tests still prove:

- complete validated answer is committed before SSE hint;
- human takeover suppresses late AI publication;
- duplicate chat job execution creates no duplicate answer;
- approval stays bound to the exact draft version;
- duplicate delivery intent is idempotent.

- [ ] **Step 2: Run and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/core/test_celery_database.py tests/integration/operations/test_readiness.py tests/integration/chat/test_answer_before_stream.py tests/integration/support/test_answer_triggers.py tests/integration/email/test_tasks.py -q`

Expected: FAIL on Claude/Anthropic wiring expectations.

- [ ] **Step 3: Replace runtime constructors without moving business logic**

- `_build_safety_classifier()` builds `OpenAIStructuredSafetyClassifier` from the configured OpenAI classifier model.
- `_build_classifier()` builds `OpenAIEmailClassifier`.
- App startup enables grounded answers when OpenAI and Redis are configured.
- Readiness dependency key becomes `openai` and covers AI answers, classification, and drafting.
- Existing unavailable objects fail closed when OpenAI is absent.
- Do not change `ChatAnswerService`, `EmailReviewService`, or `EmailDeliveryService` state/transaction ordering except for new usage fields.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2 plus:

`cd backend && .venv/bin/python -m pytest tests/integration/chat/test_chat_job_recovery.py tests/integration/email/test_review_conflicts.py tests/integration/email/test_delivery_intent.py -q`

Expected: PASS when the isolated PostgreSQL test service is available.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/chat/tasks.py backend/app/modules/email/tasks.py backend/app/main.py backend/app/modules/operations/health.py backend/tests/unit/core/test_celery_database.py backend/tests/integration/operations/test_readiness.py backend/tests/integration/chat backend/tests/integration/support/test_answer_triggers.py backend/tests/integration/email/test_tasks.py backend/tests/integration/email/test_review_conflicts.py backend/tests/integration/email/test_delivery_intent.py
git commit -m "feat: wire OpenAI across active AI paths"
```

### Task 9: Remove obsolete Anthropic code and update operational documentation

**Files:**
- Modify: `README.md`
- Modify: `compose.yaml`
- Modify: `compose.test.yaml`
- Modify: `docs/architecture/overview.md`
- Modify: `docs/architecture/security-boundaries.md`
- Modify: `docs/deployment/credential-ownership.md`
- Modify: `docs/deployment/production.md`
- Modify: `docs/readiness/checklist.md`
- Modify: `docs/runbooks/observability.md`
- Modify: `docs/runbooks/customer-chat.md`
- Modify: `docs/runbooks/email-triage.md`
- Modify: `docs/runbooks/retrieval.md`
- Modify: `scripts/check-documentation`
- Modify/remove: Anthropic-specific fakes and tests found by the reference scan

- [ ] **Step 1: Add a failing documentation/reference check**

Extend `scripts/check-documentation` or a focused unit test so active code/config/docs fail when they contain runtime Anthropic identifiers. Historical design/plans and migration records are excluded deliberately.

Run:

```bash
rg -n -i "anthropic|claude" backend/app backend/pyproject.toml .env.example compose.yaml compose.test.yaml README.md docs/architecture docs/deployment docs/readiness docs/runbooks scripts
```

Expected before cleanup: matches in active runtime and docs.

- [ ] **Step 2: Update docs and configuration**

Document:

- LlamaIndex/LangChain/LangGraph ownership and execution flow;
- `OPENAI_API_KEY`, generation/classifier/embedding model variables, timeouts, graph budget, and cost rates;
- removal of Anthropic runtime requirements and lack of automatic fallback;
- local fake-provider verification with no paid calls;
- no schema migration/re-embedding;
- rollback to the prior application revision and prior provider configuration;
- limitation that optional reranking remains disabled until evaluated.

Rename fake provider routes/helpers from Anthropic to OpenAI and update expected model labels without rewriting historical persisted fixtures where the old model name is intentionally data.

- [ ] **Step 3: Verify reference cleanup**

Run the `rg` command from Step 1. Expected: no active runtime/config/documentation matches; only explicitly historical test fixtures/specs may match in a separately reviewed list.

Run: `scripts/check-documentation`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md compose.yaml compose.test.yaml docs scripts backend/tests backend/app backend/pyproject.toml .env.example
git commit -m "docs: document OpenAI framework architecture and rollback"
```

### Task 10: Run complete verification and report infrastructure gaps

**Files:**
- Modify only files required to correct failures introduced by Tasks 1-9.

- [ ] **Step 1: Run targeted AI tests**

```bash
cd backend
.venv/bin/python -m pytest \
  tests/unit/core/test_openai_config.py \
  tests/unit/core/test_openai_structured.py \
  tests/unit/rag/test_openai_provider.py \
  tests/unit/rag/test_answer_workflow.py \
  tests/unit/rag/test_groundedness.py \
  tests/unit/email/test_classification_schema.py \
  tests/unit/support/test_triggers.py \
  tests/integration/rag/test_answer_service.py \
  -q
```

Expected: all targeted tests PASS.

- [ ] **Step 2: Run backend unit tests**

Run: `cd backend && .venv/bin/python -m pytest --import-mode=importlib tests/unit -q --ignore=app/Test.py`

Expected: all project-owned unit tests PASS. Record separately that the repository's Make target still discovers the user's untracked `app/Test.py` for lint/typecheck, not pytest.

- [ ] **Step 3: Run isolated integration tests**

Build the disposable test image and run the scoped integration suites inside the Compose network:

```bash
docker compose -f compose.test.yaml build backend-test
docker compose -f compose.test.yaml run --rm backend-test \
  python -m pytest --import-mode=importlib \
  tests/integration/rag \
  tests/integration/chat \
  tests/integration/support \
  tests/integration/email \
  tests/integration/operations/test_readiness.py \
  -q
```

Expected: PASS. If container infrastructure is unavailable, report the exact connection/startup error rather than claiming coverage.

- [ ] **Step 4: Run lint and type checks without modifying the user file**

```bash
cd backend
.venv/bin/python -m ruff check app tests --exclude app/Test.py
.venv/bin/python -m mypy app --exclude 'app/Test.py'
```

Expected: PASS.

- [ ] **Step 5: Run frontend compatibility checks**

```bash
cd frontend
npm test -- --run
npm run lint
npm run typecheck
npm run build
```

Expected: tests, lint, type checking, and production build PASS without frontend contract changes.

- [ ] **Step 6: Run API/documentation checks and final reference scan**

```bash
scripts/check-documentation
rg -n -i "anthropic|claude" backend/app backend/pyproject.toml .env.example compose.yaml compose.test.yaml README.md docs/architecture docs/deployment docs/readiness docs/runbooks scripts
git diff --check master...HEAD
git status --short --branch
```

Expected: documentation check and diff check PASS; no active Anthropic runtime references; only the four pre-existing user-owned untracked paths remain untracked.

- [ ] **Step 7: Run security-focused end-to-end provider tests**

Run:

```bash
docker compose -f compose.test.yaml build backend-e2e
docker compose -f compose.test.yaml run --rm backend-e2e \
  python -m pytest --import-mode=importlib \
  tests/e2e/test_provider_failures.py \
  tests/e2e/test_release_security_gates.py \
  tests/e2e/test_chat_to_human_handoff.py \
  tests/e2e/test_gmail_to_reviewed_delivery.py \
  -q
```

Expected: PASS using only repository fake providers and disposable PostgreSQL/Redis.

- [ ] **Step 8: Review requirements line by line**

Confirm from code/tests that:

- branch and unrelated work are preserved;
- no production data, real email, paid model call, merge, push, or deploy occurred;
- all active generation/classification is OpenAI through LangChain;
- LlamaIndex performs actual authorized fusion;
- LangGraph owns the bounded answer flow without checkpoint persistence;
- API/SSE, handoff, job, Outbox, approval, delivery, and audit behavior remains intact;
- no database migration or re-embedding occurred;
- README/docs include setup, verification, limitations, and rollback.

- [ ] **Step 9: Commit verification-only corrections, if any**

Run `git status --short`, stage each listed migration-owned correction by its explicit path,
then run `git commit -m "test: complete AI framework migration verification"`. Skip this commit
when verification required no changes; never use `git add -A` because the worktree contains
user-owned untracked files.
