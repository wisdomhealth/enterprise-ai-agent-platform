from collections.abc import Mapping

import structlog
from structlog.typing import EventDict, WrappedLogger

_SECRET_FIELD_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
_REDACTED = "[REDACTED]"


def _redact(key: str, value: object) -> object:
    if any(part in key.casefold() for part in _SECRET_FIELD_PARTS):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact(str(child_key), child)
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(key, item) for item in value]
    return value


def redact_secret_fields(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    return {key: _redact(key, value) for key, value in event_dict.items()}


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secret_fields,
            structlog.processors.JSONRenderer(),
        ],
        cache_logger_on_first_use=True,
    )
