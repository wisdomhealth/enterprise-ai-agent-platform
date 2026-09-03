from pathlib import Path

from pydantic import AnyUrl, Field, PostgresDsn, SecretStr
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
    migration_database_url: PostgresDsn | None = Field(
        default=None,
        validation_alias="MIGRATION_DATABASE_URL",
    )
    redis_url: AnyUrl | None = Field(default=None, validation_alias="REDIS_URL")
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-3-5-sonnet-latest", validation_alias="ANTHROPIC_MODEL"
    )
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
    erasure_hash_key: SecretStr | None = Field(
        default=None, validation_alias="ERASURE_HASH_KEY"
    )
    staff_session_ttl_seconds: int = Field(
        default=28_800,
        gt=0,
        validation_alias="STAFF_SESSION_TTL_SECONDS",
    )
    public_base_url: AnyUrl | None = Field(default=None, validation_alias="PUBLIC_BASE_URL")
    internal_base_url: AnyUrl | None = Field(default=None, validation_alias="INTERNAL_BASE_URL")
