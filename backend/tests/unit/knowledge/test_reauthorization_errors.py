from types import SimpleNamespace
from uuid import uuid4

import pytest
from google.auth.exceptions import RefreshError

from app.modules.knowledge.service import (
    DriveReauthorizationRequired,
    KnowledgeSourceService,
)


class GoogleHttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"Google HTTP {status}")
        self.resp = SimpleNamespace(status=status)


def _classify(error: Exception) -> None:
    KnowledgeSourceService._raise_drive_reauthorization(
        error,
        uuid4(),
        uuid4(),
    )


def test_google_401_requires_drive_reauthorization() -> None:
    with pytest.raises(DriveReauthorizationRequired):
        _classify(GoogleHttpError(401))


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_google_permission_rate_limit_and_server_errors_do_not_require_reauthorization(
    status: int,
) -> None:
    _classify(GoogleHttpError(status))


def test_retryable_refresh_error_does_not_require_reauthorization() -> None:
    _classify(
        RefreshError(
            "server_error: temporarily unavailable",
            {"error": "server_error"},
            retryable=True,
        )
    )
