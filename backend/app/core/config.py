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
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
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
    staff_session_ttl_seconds: int = Field(
        default=28_800,
        gt=0,
        validation_alias="STAFF_SESSION_TTL_SECONDS",
    )
    public_base_url: AnyUrl | None = Field(default=None, validation_alias="PUBLIC_BASE_URL")
    internal_base_url: AnyUrl | None = Field(default=None, validation_alias="INTERNAL_BASE_URL")
