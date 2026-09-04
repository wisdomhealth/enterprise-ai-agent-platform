import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def test_async_database_suite_uses_one_pytest_event_loop_scope() -> None:
    configuration = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text())
    pytest_options = configuration["tool"]["pytest"]["ini_options"]

    assert pytest_options["asyncio_default_fixture_loop_scope"] == "session"
    assert pytest_options["asyncio_default_test_loop_scope"] == "session"
