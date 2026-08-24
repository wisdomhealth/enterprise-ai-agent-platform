from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "platform_http_requests_total",
    "HTTP requests handled by the platform.",
    ("method", "path", "status"),
)
RAG_ANSWER_GENERATIONS = Counter(
    "platform_rag_answer_generations_total",
    "Validated RAG answer generation attempts without prompt or answer content.",
    ("audience", "model", "prompt_version", "outcome"),
)
RAG_ANSWER_LATENCY_SECONDS = Histogram(
    "platform_rag_answer_latency_seconds",
    "End-to-end grounded answer latency without prompt or answer content.",
    ("audience", "model", "prompt_version", "outcome"),
)
RAG_ANSWER_TOKENS = Counter(
    "platform_rag_answer_tokens_total",
    "Provider token counts without prompt or answer content.",
    ("model", "direction"),
)
RAG_ANSWER_ESTIMATED_COST = Counter(
    "platform_rag_answer_estimated_cost_total",
    "Estimated provider cost without prompt or answer content.",
    ("model",),
)
RAG_RETRIEVED_CHUNKS = Histogram(
    "platform_rag_retrieved_chunks",
    "Authorized retrieved chunk count without document or prompt content.",
    ("audience", "prompt_version"),
)


def record_http_request(method: str, path: str, status_code: int) -> None:
    HTTP_REQUESTS.labels(method=method, path=path, status=str(status_code)).inc()


def record_grounded_answer(
    *,
    audience: str,
    model: str,
    prompt_version: str,
    outcome: str,
    retrieved_chunk_count: int,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
    estimated_cost: float,
) -> None:
    """Record only operational metadata; prompts and generated text are intentionally omitted."""

    labels = {
        "audience": audience,
        "model": model,
        "prompt_version": prompt_version,
        "outcome": outcome,
    }
    RAG_ANSWER_GENERATIONS.labels(**labels).inc()
    RAG_ANSWER_LATENCY_SECONDS.labels(**labels).observe(latency_ms / 1_000)
    RAG_RETRIEVED_CHUNKS.labels(audience=audience, prompt_version=prompt_version).observe(
        retrieved_chunk_count
    )
    RAG_ANSWER_TOKENS.labels(model=model, direction="input").inc(input_tokens)
    RAG_ANSWER_TOKENS.labels(model=model, direction="output").inc(output_tokens)
    RAG_ANSWER_ESTIMATED_COST.labels(model=model).inc(estimated_cost)


def prometheus_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
