import json
import os
import runpy
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fakes.providers import HttpStructuredAnswerProvider

from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.rag.answer_service import GroundedAnswerService
from app.modules.rag.groundedness import CitationValidator
from app.modules.rag.llm import InMemoryRedisCircuitStore, ProviderCircuitBreaker
from app.modules.rag.types import AnswerAudience, RetrievedChunk

ROOT = Path(__file__).resolve().parents[3]


class FixedRetriever:
    def __init__(self, chunk: RetrievedChunk) -> None:
        self._chunk = chunk

    async def retrieve(self, *_args: object, **_kwargs: object) -> list[RetrievedChunk]:
        return [self._chunk]


def _context() -> tuple[Principal, RetrievedChunk]:
    principal = Principal(
        uuid4(), uuid4(), "customer@example.test", UserRole.MEMBER, uuid4(), "task26"
    )
    chunk = RetrievedChunk(
        chunk_id=uuid4(),
        stable_id="policy-1",
        document_version_id=uuid4(),
        document_id=uuid4(),
        organization_id=principal.organization_id,
        knowledge_base_id=uuid4(),
        ordinal=0,
        text="Refunds take five business days.",
        page_number=2,
        section="Refunds",
        resource_authorized=True,
        title="Customer policy",
        internal_drive_link="https://drive.google.test/internal",
    )
    return principal, chunk


@pytest.mark.asyncio
async def test_customer_never_receives_unvalidated_or_internal_source_data(provider_stack) -> None:  # type: ignore[no-untyped-def]
    principal, chunk = _context()
    provider_stack.queue_anthropic_answer(
        text="Refunds take five business days.",
        claims=[
            {"text": "Refunds take five business days.", "citation_ids": [str(chunk.chunk_id)]}
        ],
    )
    async with provider_stack.client("anthropic") as client:
        service = GroundedAnswerService(
            FixedRetriever(chunk),
            HttpStructuredAnswerProvider(client),  # type: ignore[arg-type]
            CitationValidator(),
            ProviderCircuitBreaker(InMemoryRedisCircuitStore()),
        )
        answer = await service.answer(
            principal, chunk.knowledge_base_id, "When are refunds paid?", AnswerAudience.CUSTOMER
        )

    assert answer.refused is False
    assert answer.citations
    assert answer.citations[0].model_dump() == {
        "title": "Customer policy",
        "section": "Refunds",
        "page_number": 2,
    }
    assert "drive.google" not in str(answer.model_dump())
    assert str(chunk.chunk_id) not in str(answer.citations)


@pytest.mark.asyncio
async def test_unsupported_provider_claim_is_blocked_before_customer_output(provider_stack) -> None:  # type: ignore[no-untyped-def]
    principal, chunk = _context()
    provider_stack.queue_anthropic_answer(
        text="Refunds take one hour.",
        claims=[{"text": "Refunds take one hour.", "citation_ids": [str(chunk.chunk_id)]}],
    )
    async with provider_stack.client("anthropic") as client:
        answer = await GroundedAnswerService(
            FixedRetriever(chunk),
            HttpStructuredAnswerProvider(client),  # type: ignore[arg-type]
            CitationValidator(),
            ProviderCircuitBreaker(InMemoryRedisCircuitStore()),
        ).answer(
            principal, chunk.knowledge_base_id, "When are refunds paid?", AnswerAudience.CUSTOMER
        )

    assert answer.refused is True
    assert "one hour" not in answer.text
    assert answer.citations == []


def test_release_gate_plan_separates_hard_gates_from_quality_targets(tmp_path: Path) -> None:
    evidence = tmp_path / "release.json"
    result = subprocess.run(
        [
            str(ROOT / "scripts/verify-release-gates"),
            "--compose-file",
            "compose.test.yaml",
            "--evidence",
            str(evidence),
            "--plan-only",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["status"] == "PLANNED"
    assert payload["hard_gates"]
    assert all(gate["status"] == "PLANNED" for gate in payload["hard_gates"])
    assert payload["quality_targets"]["release_blocking"] is False


def test_browser_release_journeys_use_one_worker_for_shared_durable_fixture() -> None:
    config = (ROOT / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")

    assert "workers: 1" in config


def test_frontend_unit_image_includes_the_source_imported_by_tests() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY components ./components" in dockerfile
    assert "COPY lib ./lib" in dockerfile


def test_integration_gate_uses_the_migration_principal_only_for_schema_regressions() -> None:
    verifier = runpy.run_path(str(ROOT / "scripts" / "verify-release-gates"))
    integration = next(
        gate for gate in verifier["_gates"](ROOT / "compose.test.yaml")
        if gate.name == "backend_integration"
    )

    command = " ".join(integration.command)
    assert "tests/integration" in command
    assert "MIGRATION_DATABASE_URL" in command
    assert "--ignore" not in command


def test_durable_fixture_gates_use_the_migration_principal() -> None:
    verifier = runpy.run_path(str(ROOT / "scripts" / "verify-release-gates"))
    gates = {gate.name: gate for gate in verifier["_gates"](ROOT / "compose.test.yaml")}

    for name in ("backend_e2e", "erasure_replay"):
        command = " ".join(gates[name].command)
        assert "MIGRATION_DATABASE_URL" in command


def test_benchmark_records_baseline_without_asserting_an_sla(tmp_path: Path) -> None:
    evidence = tmp_path / "capacity.json"
    result = subprocess.run(
        [
            str(ROOT / "scripts/benchmark-baseline"),
            "--compose-file",
            "compose.test.yaml",
            "--documents",
            "10",
            "--staff",
            "2",
            "--daily-emails",
            "3",
            "--evidence",
            str(evidence),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["workload"] == {"daily_emails": 3, "documents": 10, "staff": 2}
    assert payload["sla_asserted"] is False
    assert set(payload["measurements"]) == {
        "end_to_end",
        "estimated_cost",
        "fusion",
        "ingestion",
        "model_fake",
        "queue",
        "text_retrieval",
        "vector_retrieval",
    }


def test_release_gate_execution_rejects_an_unscoped_database(tmp_path: Path) -> None:
    evidence = tmp_path / "unsafe.json"
    environment = os.environ.copy()
    environment.update(
        {
            "TASK26_RELEASE_GATE_AUTHORIZED": "task26-release-gates",
            "TASK26_DATABASE_NAME": "platform_task26_fix",
            "DATABASE_URL": "postgresql+asyncpg://platform_app@postgres/shared_database",
            "MIGRATION_DATABASE_URL": "postgresql+asyncpg://postgres@postgres/shared_database",
            "TASK16_E2E_DATABASE_URL": (
                "postgresql+asyncpg://postgres@127.0.0.1:55436/shared_database"
            ),
        }
    )
    result = subprocess.run(
        [
            str(ROOT / "scripts/verify-release-gates"),
            "--compose-file",
            "compose.test.yaml",
            "--evidence",
            str(evidence),
            "--gate",
            "compose_config",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "platform_task26_fix" in result.stderr
    assert not evidence.exists()
