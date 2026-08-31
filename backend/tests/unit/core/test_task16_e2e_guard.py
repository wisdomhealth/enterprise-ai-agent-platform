import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.task16_e2e_guard import _EXTERNAL_PROVIDER_ENV, validate_task16_e2e_environment


def _environment(**overrides: str) -> dict[str, str]:
    return {
        "TASK16_E2E": "1",
        "APP_ENV": "test",
        "DATABASE_URL": (
            "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/"
            "platform_task15_fix"
        ),
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"TASK16_E2E": "0"}, "TASK16_E2E=1"),
        ({"APP_ENV": ""}, "APP_ENV=test"),
        ({"APP_ENV": "TEST"}, "APP_ENV=test"),
        ({"APP_ENV": "development"}, "APP_ENV=test"),
        ({"APP_ENV": "production"}, "production"),
        (
            {"DATABASE_URL": "postgresql://postgres@127.0.0.1/test_task16"},
            "postgresql\\+asyncpg",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://postgres@db.example.com/test_task16"},
            "loopback",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://postgres@127.0.0.1/platform"},
            "disposable",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+asyncpg://postgres@127.0.0.1/test_task16?host=db.example.com"
                )
            },
            "disposable",
        ),
        ({"ANTHROPIC_API_KEY": "real-provider-secret"}, "ANTHROPIC_API_KEY"),
    ],
)
def test_e2e_environment_rejects_unsafe_configuration(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_task16_e2e_environment(_environment(**overrides))


def test_e2e_environment_accepts_explicit_local_disposable_database() -> None:
    assert validate_task16_e2e_environment(_environment()).endswith("/platform_task15_fix")


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"TASK16_E2E": "0"}, "TASK16_E2E=1"),
        ({"TASK16_E2E": "1", "APP_ENV": "development"}, "APP_ENV=test"),
        (
            {
                "TASK16_E2E": "1",
                "APP_ENV": "test",
                "ANTHROPIC_API_KEY": "provider-secret",
            },
            "ANTHROPIC_API_KEY",
        ),
    ],
)
def test_unsafe_environment_fails_before_database_or_app_import(
    updates: dict[str, str], message: str
) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {*_EXTERNAL_PROVIDER_ENV, "APP_ENV", "DATABASE_URL", "TASK16_E2E"}
    }
    environment["DATABASE_URL"] = _environment()["DATABASE_URL"]
    environment.update(updates)
    result = subprocess.run(
        [sys.executable, "-c", "import tests.task16_e2e_app"],
        capture_output=True,
        check=False,
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert "DATABASE_URL is required to configure the database" not in result.stderr


def test_e2e_app_invokes_guard_before_database_or_application_import() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {*_EXTERNAL_PROVIDER_ENV, "APP_ENV", "DATABASE_URL", "TASK16_E2E"}
    }
    environment.update(_environment())
    script = """
import builtins
import importlib

import tests.task16_e2e_guard as guard

events = []
real_import = builtins.__import__

class GuardReached(Exception):
    pass

def record_guard(environ):
    events.append("guard")
    raise GuardReached

def track_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name in {"app.core.database", "app.main"}:
        if events != ["guard"]:
            raise AssertionError(f"production import occurred before guard: {name}")
        events.append(name)
    return real_import(name, globals, locals, fromlist, level)

guard.validate_task16_e2e_environment = record_guard
builtins.__import__ = track_import
try:
    importlib.import_module("tests.task16_e2e_app")
except GuardReached:
    pass
else:
    raise AssertionError("Task 16 guard was not invoked")

if events != ["guard"]:
    raise AssertionError(f"unexpected import order: {events}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
