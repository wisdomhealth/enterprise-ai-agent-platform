import json
import logging
from io import StringIO

from app.core.logging import configure_logging


def test_stdlib_and_uvicorn_logs_are_json_and_redacted() -> None:
    output = StringIO()
    secret = "runtime-super-secret"
    configure_logging(stream=output)

    logging.getLogger("platform.runtime").warning(
        "stdlib event",
        extra={"api_key": secret},
    )
    logging.getLogger("uvicorn.error").error(
        "server event",
        extra={"session_secret": secret},
    )
    logging.getLogger("uvicorn.access").info(
        "access event",
        extra={"authorization": secret},
    )

    rendered = output.getvalue()
    records = [json.loads(line) for line in rendered.splitlines()]

    assert secret not in rendered
    assert {record["logger"] for record in records} == {
        "platform.runtime",
        "uvicorn.access",
        "uvicorn.error",
    }
    assert [record["event"] for record in records] == [
        "stdlib event",
        "server event",
        "access event",
    ]
    assert all("[REDACTED]" in record.values() for record in records)
