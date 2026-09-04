from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import ConversationState
from app.modules.email.models import DeliveryIntent, EmailState, EmailWorkItem
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.models import DriveSource
from app.modules.retention.models import ErasureRequest, ErasureStatus
from app.modules.support.models import Handoff

HTTP_REQUESTS = Counter(
    "platform_http_requests_total",
    "HTTP requests handled by the platform.",
    ("method", "path", "status"),
)
HTTP_REQUEST_LATENCY_SECONDS = Histogram(
    "platform_http_request_latency_seconds",
    "HTTP request latency without request or response bodies.",
    ("method", "path"),
)
RAG_RETRIEVAL_LATENCY_SECONDS = Histogram(
    "platform_rag_retrieval_latency_seconds",
    "Authorized retrieval latency without query or document content.",
)
RAG_MODEL_LATENCY_SECONDS = Histogram(
    "platform_rag_model_latency_seconds",
    "Model latency without prompt or generated content.",
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
DEPENDENCY_UP = Gauge(
    "platform_dependency_up",
    "Whether an operational dependency is available.",
    ("dependency",),
)
CONNECTOR_STALENESS_SECONDS = Gauge(
    "platform_connector_staleness_seconds",
    "Age in seconds of the stalest successfully synchronized connector.",
)
JOB_BACKLOG = Gauge("platform_job_backlog", "Durable jobs awaiting terminal completion.")
JOB_RETRIES = Gauge("platform_job_retries", "Accumulated durable job attempts beyond the first.")
EXPIRED_JOB_LEASES = Gauge(
    "platform_expired_job_leases", "Running jobs whose database-time lease has expired."
)
SUPPORT_HANDOFF_BACKLOG = Gauge(
    "platform_support_handoff_backlog", "Queued or requested human support handoffs."
)
EMAIL_STATE = Gauge(
    "platform_email_state",
    "Durable email work items grouped by safe lifecycle state.",
    ("state",),
)
EMAIL_DELIVERY_UNKNOWN = Gauge(
    "platform_email_delivery_unknown", "Email delivery intents awaiting reconciliation."
)
EMAIL_DELIVERY_UNKNOWN_OLDEST_AGE_SECONDS = Gauge(
    "platform_email_delivery_unknown_oldest_age_seconds",
    "Age of the oldest delivery-unknown intent in seconds.",
)
ERASURE_BACKLOG = Gauge(
    "platform_erasure_backlog",
    "Erasure requests not fully applied for the active restore generation.",
)

# No-content series make every required metric discoverable before first traffic.
RAG_RETRIEVAL_LATENCY_SECONDS.observe(0)
RAG_MODEL_LATENCY_SECONDS.observe(0)


def record_http_request(method: str, path: str, status_code: int) -> None:
    HTTP_REQUESTS.labels(method=method, path=path, status=str(status_code)).inc()


async def refresh_operational_metrics(
    db_session: AsyncSession,
    *,
    restore_generation: int,
) -> None:
    """Refresh bounded, aggregate-only PostgreSQL metrics without tenant labels."""

    database_now = func.clock_timestamp()
    job_backlog, retries, expired_leases = (
        await db_session.execute(
            select(
                func.count(JobIntent.id).filter(
                    JobIntent.state.in_(
                        [JobState.PENDING, JobState.RUNNING, JobState.RECONCILIATION]
                    )
                ),
                func.coalesce(func.sum(func.greatest(JobIntent.attempts - 1, 0)), 0),
                func.count(JobIntent.id).filter(
                    JobIntent.state == JobState.RUNNING,
                    JobIntent.lease_expires_at.is_not(None),
                    JobIntent.lease_expires_at <= database_now,
                ),
            )
        )
    ).one()
    JOB_BACKLOG.set(int(job_backlog or 0))
    JOB_RETRIES.set(int(retries or 0))
    EXPIRED_JOB_LEASES.set(int(expired_leases or 0))

    source_key = JobIntent.payload["source_id"].as_string()
    latest_by_source = (
        select(
            source_key.label("source_id"),
            func.max(JobIntent.updated_at).label("last_success"),
        )
        .where(
            JobIntent.kind == "knowledge.drive_source.sync",
            JobIntent.state == JobState.SUCCEEDED,
        )
        .group_by(source_key)
        .subquery()
    )
    oldest_drive_checkpoint = await db_session.scalar(
        select(func.min(func.coalesce(latest_by_source.c.last_success, DriveSource.created_at)))
        .select_from(DriveSource)
        .outerjoin(
            latest_by_source,
            latest_by_source.c.source_id == cast(DriveSource.id, String),
        )
    )
    staleness = 0.0
    if oldest_drive_checkpoint is not None:
        staleness = float(
            await db_session.scalar(
                select(func.extract("epoch", database_now - oldest_drive_checkpoint))
            )
            or 0
        )
    CONNECTOR_STALENESS_SECONDS.set(max(0.0, staleness))

    handoff_backlog = await db_session.scalar(
        select(func.count(Handoff.id)).where(
            Handoff.state.in_([ConversationState.HANDOFF_REQUESTED, ConversationState.QUEUED])
        )
    )
    SUPPORT_HANDOFF_BACKLOG.set(int(handoff_backlog or 0))

    email_count_rows = (
        await db_session.execute(
            select(EmailWorkItem.state, func.count(EmailWorkItem.id)).group_by(EmailWorkItem.state)
        )
    ).all()
    email_counts: dict[EmailState, int] = {row[0]: int(row[1]) for row in email_count_rows}
    for state in EmailState:
        EMAIL_STATE.labels(state=state.value).set(int(email_counts.get(state, 0)))

    unknown_count, oldest_unknown = (
        await db_session.execute(
            select(
                func.count(DeliveryIntent.id),
                func.min(DeliveryIntent.updated_at),
            ).where(DeliveryIntent.state == EmailState.DELIVERY_UNKNOWN)
        )
    ).one()
    EMAIL_DELIVERY_UNKNOWN.set(int(unknown_count or 0))
    unknown_age = 0.0
    if oldest_unknown is not None:
        unknown_age = float(
            await db_session.scalar(select(func.extract("epoch", database_now - oldest_unknown)))
            or 0
        )
    EMAIL_DELIVERY_UNKNOWN_OLDEST_AGE_SECONDS.set(max(0.0, unknown_age))

    erasure_filters = [ErasureRequest.status != ErasureStatus.APPLIED]
    if restore_generation > 0:
        erasure_filters.append(ErasureRequest.replay_generation < restore_generation)
    erasure_backlog = await db_session.scalar(
        select(func.count(ErasureRequest.id)).where(or_(*erasure_filters))
    )
    ERASURE_BACKLOG.set(int(erasure_backlog or 0))


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


def record_retrieval_latency(latency_ms: int) -> None:
    RAG_RETRIEVAL_LATENCY_SECONDS.observe(max(0, latency_ms) / 1_000)


def record_model_latency(latency_ms: int) -> None:
    RAG_MODEL_LATENCY_SECONDS.observe(max(0, latency_ms) / 1_000)


def prometheus_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
