"""Fail-closed environment gate for the Task 16/20 test-only ASGI harness."""

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
_TASK26_LOCAL_RUNTIME_KEYS = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "REDIS_URL"})


def _is_task26_local_provider_url(value: str, *, expected_path: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port == 3201
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == expected_path.rstrip("/")
        and not parsed.query
        and not parsed.fragment
    )


def _has_only_task26_local_provider(environ: Mapping[str, str]) -> bool:
    return (
        environ.get("TASK26_LOCAL_PROVIDER") == "1"
        and environ.get("ANTHROPIC_API_KEY") == "task26-local"
        and environ.get("OPENAI_API_KEY") == "task26-local"
        and _is_task26_local_provider_url(
            environ.get("ANTHROPIC_BASE_URL", ""), expected_path=""
        )
        and _is_task26_local_provider_url(
            environ.get("OPENAI_BASE_URL", ""), expected_path="/v1"
        )
        and environ.get("REDIS_URL") == "redis://127.0.0.1:56385/0"
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
    disposable_name = database_name.startswith(("test_", "task16_")) or database_name in {
        "platform_task15_fix",
        "platform_task20_fix",
        "platform_task26_fix",
    }
    if not disposable_name or "/" in database_name or parsed.query or parsed.fragment:
        raise RuntimeError("Task 16 test harness requires a disposable database name")

    local_provider = _has_only_task26_local_provider(environ)
    provider_fields = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "TASK26_LOCAL_PROVIDER",
    )
    if any(environ.get(name, "").strip() for name in provider_fields) and not local_provider:
        raise RuntimeError(
            "Task 16 test harness requires the explicit loopback fake provider configuration"
        )
    for name in _EXTERNAL_PROVIDER_ENV:
        if local_provider and name in _TASK26_LOCAL_RUNTIME_KEYS:
            continue
        if environ.get(name, "").strip():
            raise RuntimeError(f"Task 16 test harness requires {name} to be unset")
    return database_url
