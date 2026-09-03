from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> dict[str, object]:
        parsed = json.loads(self.stdout)
        assert isinstance(parsed, dict)
        return parsed


class RecoveryCli:
    def run(
        self,
        script: str,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> ScriptResult:
        process_env = os.environ.copy()
        process_env.update(env or {})
        completed = subprocess.run(
            [str(ROOT / "scripts" / script), *arguments],
            cwd=ROOT,
            env=process_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return ScriptResult(completed.returncode, completed.stdout, completed.stderr)


@pytest.fixture
def recovery_cli() -> RecoveryCli:
    return RecoveryCli()
