"""Fail-closed environment gate for the Task 16 test-only ASGI harness."""

from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_EXTERNAL_PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_OIDC_CLIENT_ID",
    "GOOGLE_OIDC_CLIENT_SECRET",
    "GOOGLE_DRIVE_CLIENT_ID",
    "GOOGLE_DRIVE_CLIENT_SECRET",
    "GOOGLE_GMAIL_CLIENT_ID",
    "GOOGLE_GMAIL_CLIENT_SECRET",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_KMS_KEY_NAME",
    "CONNECTOR_FILE_KEY_PATH",
    "REDIS_URL",
)


def validate_task16_e2e_environment(environ: Mapping[str, str]) -> str:
    if environ.get("TASK16_E2E") != "1":
        raise RuntimeError("Task 16 test harness requires TASK16_E2E=1")
    if environ.get("APP_ENV") != "test":
        raise RuntimeError(
            "Task 16 test harness requires APP_ENV=test and refuses production/non-test APP_ENV"
        )

    database_url = environ.get("DATABASE_URL", "")
    parsed = urlsplit(database_url)
    if parsed.scheme != "postgresql+asyncpg":
        raise RuntimeError("Task 16 test harness requires a postgresql+asyncpg DATABASE_URL")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise RuntimeError("Task 16 test harness requires a loopback database host")
    database_name = unquote(parsed.path.removeprefix("/"))
    disposable_name = database_name.startswith(("test_", "task16_")) or (
        database_name == "platform_task15_fix"
    )
    if not disposable_name or "/" in database_name or parsed.query or parsed.fragment:
        raise RuntimeError("Task 16 test harness requires a disposable database name")

    for name in _EXTERNAL_PROVIDER_ENV:
        if environ.get(name, "").strip():
            raise RuntimeError(f"Task 16 test harness requires {name} to be unset")
    return database_url
