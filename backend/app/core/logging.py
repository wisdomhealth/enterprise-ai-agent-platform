import logging
from collections.abc import Mapping
from typing import TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_SECRET_FIELD_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
_CONTENT_FIELDS = frozenset(
    {
        "answer",
        "body",
        "chat_body",
        "completion",
        "content",
        "document_content",
        "document_text",
        "email_body",
        "message_body",
        "prompt",
        "query",
        "raw_body",
        "response_body",
    }
)
_REDACTED = "[REDACTED]"


class _AccessLogQueryFilter(logging.Filter):
    """Keep access metadata while removing credentials commonly carried in query strings."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            arguments = list(record.args)
            arguments[2] = str(arguments[2]).partition("?")[0]
            record.args = tuple(arguments)
        return True


def _redact(key: str, value: object) -> object:
    normalized_key = key.casefold()
    if any(part in normalized_key for part in _SECRET_FIELD_PARTS) or (
        normalized_key in _CONTENT_FIELDS
        or normalized_key.endswith("_body")
        or normalized_key.endswith("_content")
        or normalized_key.endswith("_text")
    ):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact(str(child_key), child) for child_key, child in value.items()
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


def log_event(event: str, **fields: object) -> None:
    """Emit an allowlisted-by-redaction structured event without interpolated content."""

    structlog.get_logger("platform").info(event, **fields)


def configure_logging(stream: TextIO | None = None) -> None:
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secret_fields,
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *shared_processors],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = True

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = False
    access_logger.filters.clear()
    access_logger.addFilter(_AccessLogQueryFilter())
