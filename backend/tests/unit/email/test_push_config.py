from app.core.config import Settings
from app.main import _missing_gmail_push_settings


def test_gmail_push_and_draft_retry_defaults_are_safe() -> None:
    settings = Settings()

    assert settings.gmail_push_enabled is False
    assert settings.gmail_watch_renewal_interval_seconds == 24 * 60 * 60
    assert settings.gmail_reconciliation_interval_seconds == 6 * 60 * 60
    assert settings.gmail_push_receipt_retention_days == 14
    assert settings.gmail_recovery_lookback_days == 7
    assert settings.gmail_recovery_max_messages == 500
    assert settings.email_draft_max_attempts == 3
    assert settings.email_draft_retry_base_seconds == 10
    assert settings.email_draft_retry_max_seconds == 600


def test_enabled_gmail_push_reports_every_missing_required_setting() -> None:
    settings = Settings.model_validate({"GMAIL_PUSH_ENABLED": True})

    assert _missing_gmail_push_settings(settings) == (
        "GMAIL_PUBSUB_TOPIC",
        "GMAIL_PUBSUB_SUBSCRIPTION",
        "GMAIL_PUSH_ENDPOINT_URL",
        "GMAIL_PUSH_OIDC_AUDIENCE",
        "GMAIL_PUSH_SERVICE_ACCOUNT_EMAIL",
    )
    assert _missing_gmail_push_settings(Settings()) == ()
