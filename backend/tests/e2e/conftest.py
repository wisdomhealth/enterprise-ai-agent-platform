import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from fakes.providers import DeterministicProviderStack  # noqa: E402

pytest_plugins = ("integration.email.conftest",)


@pytest_asyncio.fixture
async def provider_stack() -> AsyncIterator[DeterministicProviderStack]:
    """One isolated provider state per cross-system journey."""
    yield DeterministicProviderStack()
