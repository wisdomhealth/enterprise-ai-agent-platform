from pathlib import Path
from typing import Self

from pydantic import AnyUrl, Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=None,
        env_ignore_empty=True,
        extra="forbid",
        strict=True,
    )

    database_url: PostgresDsn | None = Field(default=None, validation_alias="DATABASE_URL")
    database_pool_size: int = Field(
        default=5,
        ge=1,
        le=50,
        validation_alias="DATABASE_POOL_SIZE",
    )
    database_max_overflow: int = Field(
        default=5,
        ge=0,
        le=50,
        validation_alias="DATABASE_MAX_OVERFLOW",
    )
    api_process_count: int = Field(
        default=1,
        ge=1,
        le=50,
        validation_alias="API_PROCESS_COUNT",
    )
    celery_worker_concurrency: int = Field(
        default=4,
        ge=1,
        le=100,
        validation_alias="CELERY_WORKER_CONCURRENCY",
    )
    celery_worker_instances: int = Field(
        default=1,
        ge=1,
        le=50,
        validation_alias="CELERY_WORKER_INSTANCES",
    )
    celery_task_connection_peak: int = Field(
        default=2,
        ge=2,
        le=10,
        validation_alias="CELERY_TASK_CONNECTION_PEAK",
    )
    postgres_admin_migration_reserve: int = Field(
        default=10,
        ge=1,
        le=100,
        validation_alias="POSTGRES_ADMIN_MIGRATION_RESERVE",
    )
    postgres_max_connections: int = Field(
        default=100,
        ge=1,
        le=1000,
        validation_alias="POSTGRES_MAX_CONNECTIONS",
    )
    migration_database_url: PostgresDsn | None = Field(
        default=None,
        validation_alias="MIGRATION_DATABASE_URL",
    )
    redis_url: AnyUrl | None = Field(default=None, validation_alias="REDIS_URL")
    safety_classifier_model: str | None = Field(
        default=None,
        validation_alias="SAFETY_CLASSIFIER_MODEL",
    )
    grounded_refusal_message: str = Field(
        default=(
            "I don't know based on the available information. "
            "Please contact a team member for help."
        ),
        validation_alias="GROUNDED_REFUSAL_MESSAGE",
    )
    provider_circuit_failure_threshold: int = Field(
        default=5, ge=1, le=100, validation_alias="PROVIDER_CIRCUIT_FAILURE_THRESHOLD"
    )
    provider_circuit_reset_seconds: int = Field(
        default=30, ge=1, le=3600, validation_alias="PROVIDER_CIRCUIT_RESET_SECONDS"
    )
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: AnyUrl | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    openai_generation_model: str = Field(
        default="gpt-4.1-mini", validation_alias="OPENAI_GENERATION_MODEL"
    )
    openai_classifier_model: str = Field(
        default="gpt-4.1-mini", validation_alias="OPENAI_CLASSIFIER_MODEL"
    )
    openai_embedding_model: str = Field(
        default="text-embedding-3-small", validation_alias="OPENAI_EMBEDDING_MODEL"
    )
    openai_embedding_dimensions: int = Field(
        default=1536,
        ge=1536,
        le=1536,
        validation_alias="OPENAI_EMBEDDING_DIMENSIONS",
    )
    openai_embedding_batch_size: int = Field(
        default=100,
        ge=1,
        le=2048,
        validation_alias="OPENAI_EMBEDDING_BATCH_SIZE",
    )
    openai_embedding_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias="OPENAI_EMBEDDING_MAX_RETRIES",
    )
    knowledge_chunk_size: int = Field(
        default=500,
        ge=2,
        le=8192,
        validation_alias="KNOWLEDGE_CHUNK_SIZE",
    )
    knowledge_chunk_overlap: int = Field(
        default=64,
        ge=0,
        le=499,
        validation_alias="KNOWLEDGE_CHUNK_OVERLAP",
    )
    openai_request_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
        validation_alias="OPENAI_REQUEST_TIMEOUT_SECONDS",
    )
    rag_execution_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=300,
        validation_alias="RAG_EXECUTION_TIMEOUT_SECONDS",
    )
    rag_max_generation_attempts: int = Field(
        default=2,
        ge=1,
        le=3,
        validation_alias="RAG_MAX_GENERATION_ATTEMPTS",
    )
    email_classification_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="EMAIL_CLASSIFICATION_MAX_ATTEMPTS",
    )
    email_classification_retry_base_seconds: int = Field(
        default=5,
        ge=1,
        le=3600,
        validation_alias="EMAIL_CLASSIFICATION_RETRY_BASE_SECONDS",
    )
    email_classification_retry_max_seconds: int = Field(
        default=300,
        ge=1,
        le=86400,
        validation_alias="EMAIL_CLASSIFICATION_RETRY_MAX_SECONDS",
    )
    openai_input_cost_per_million: float = Field(
        default=0.0,
        ge=0,
        validation_alias="OPENAI_INPUT_COST_PER_MILLION",
    )
    openai_output_cost_per_million: float = Field(
        default=0.0,
        ge=0,
        validation_alias="OPENAI_OUTPUT_COST_PER_MILLION",
    )
    reranker_enabled: bool = Field(default=False, validation_alias="RERANKER_ENABLED")
    google_oidc_client_id: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_OIDC_CLIENT_ID"
    )
    google_oidc_client_secret: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_OIDC_CLIENT_SECRET"
    )
    google_drive_client_id: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_DRIVE_CLIENT_ID"
    )
    google_drive_client_secret: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_DRIVE_CLIENT_SECRET"
    )
    google_gmail_client_id: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_GMAIL_CLIENT_ID"
    )
    google_gmail_client_secret: SecretStr | None = Field(
        default=None, validation_alias="GOOGLE_GMAIL_CLIENT_SECRET"
    )
    gmail_polling_enabled: bool = Field(
        default=False, validation_alias="GMAIL_POLLING_ENABLED"
    )
    gmail_push_enabled: bool = Field(default=False, validation_alias="GMAIL_PUSH_ENABLED")
    gmail_pubsub_topic: str | None = Field(default=None, validation_alias="GMAIL_PUBSUB_TOPIC")
    gmail_pubsub_subscription: str | None = Field(
        default=None, validation_alias="GMAIL_PUBSUB_SUBSCRIPTION"
    )
    gmail_push_endpoint_url: AnyUrl | None = Field(
        default=None, validation_alias="GMAIL_PUSH_ENDPOINT_URL"
    )
    gmail_push_oidc_audience: str | None = Field(
        default=None, validation_alias="GMAIL_PUSH_OIDC_AUDIENCE"
    )
    gmail_push_service_account_email: str | None = Field(
        default=None, validation_alias="GMAIL_PUSH_SERVICE_ACCOUNT_EMAIL"
    )
    gmail_push_max_body_bytes: int = Field(
        default=65_536,
        ge=1_024,
        le=1_048_576,
        validation_alias="GMAIL_PUSH_MAX_BODY_BYTES",
    )
    gmail_watch_renewal_interval_seconds: int = Field(
        default=86_400,
        ge=3_600,
        le=604_800,
        validation_alias="GMAIL_WATCH_RENEWAL_INTERVAL_SECONDS",
    )
    gmail_watch_renew_before_seconds: int = Field(
        default=172_800,
        ge=3_600,
        le=604_800,
        validation_alias="GMAIL_WATCH_RENEW_BEFORE_SECONDS",
    )
    gmail_reconciliation_interval_seconds: int = Field(
        default=3_600,
        ge=3_600,
        le=604_800,
        validation_alias="GMAIL_RECONCILIATION_INTERVAL_SECONDS",
    )
    gmail_push_receipt_retention_days: int = Field(
        default=14,
        ge=1,
        le=90,
        validation_alias="GMAIL_PUSH_RECEIPT_RETENTION_DAYS",
    )
    gmail_recovery_lookback_days: int = Field(
        default=7,
        ge=1,
        le=90,
        validation_alias="GMAIL_RECOVERY_LOOKBACK_DAYS",
    )
    gmail_recovery_max_messages: int = Field(
        default=500,
        ge=1,
        le=10_000,
        validation_alias="GMAIL_RECOVERY_MAX_MESSAGES",
    )
    gmail_sync_max_attempts: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="GMAIL_SYNC_MAX_ATTEMPTS",
    )
    email_draft_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="EMAIL_DRAFT_MAX_ATTEMPTS",
    )
    email_draft_retry_base_seconds: int = Field(
        default=10,
        ge=1,
        le=3_600,
        validation_alias="EMAIL_DRAFT_RETRY_BASE_SECONDS",
    )
    email_draft_retry_max_seconds: int = Field(
        default=600,
        ge=1,
        le=86_400,
        validation_alias="EMAIL_DRAFT_RETRY_MAX_SECONDS",
    )
    gmail_message_id_domain: str = Field(
        default="mail.invalid",
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$",
        validation_alias="GMAIL_MESSAGE_ID_DOMAIN",
    )
    google_cloud_project: str | None = Field(default=None, validation_alias="GOOGLE_CLOUD_PROJECT")
    google_kms_key_name: str | None = Field(default=None, validation_alias="GOOGLE_KMS_KEY_NAME")
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    self_hosted_file_key_allowed: bool = Field(
        default=False, validation_alias="SELF_HOSTED_FILE_KEY_ALLOWED"
    )
    connector_file_key_path: Path | None = Field(
        default=None, validation_alias="CONNECTOR_FILE_KEY_PATH"
    )
    session_secret: SecretStr | None = Field(default=None, validation_alias="SESSION_SECRET")
    erasure_hash_key: SecretStr | None = Field(default=None, validation_alias="ERASURE_HASH_KEY")
    restore_generation: int = Field(
        default=0,
        ge=0,
        validation_alias="RESTORE_GENERATION",
    )
    staff_session_ttl_seconds: int = Field(
        default=28_800,
        gt=0,
        validation_alias="STAFF_SESSION_TTL_SECONDS",
    )
    public_base_url: AnyUrl | None = Field(default=None, validation_alias="PUBLIC_BASE_URL")
    internal_base_url: AnyUrl | None = Field(default=None, validation_alias="INTERNAL_BASE_URL")

    @model_validator(mode="after")
    def validate_chunk_overlap(self) -> Self:
        if self.knowledge_chunk_overlap >= self.knowledge_chunk_size:
            raise ValueError("KNOWLEDGE_CHUNK_OVERLAP must be smaller than KNOWLEDGE_CHUNK_SIZE")
        return self
