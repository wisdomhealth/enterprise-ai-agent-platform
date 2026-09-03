from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app
from app.modules.operations.health import ConfiguredHealthReporter, DependencyStatus, HealthService


class _SessionContext:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


class _MigrationMissingSession:
    async def execute(self, statement: object) -> None:
        return None

    async def scalar(self, statement: object) -> object:
        raise RuntimeError("alembic table is absent")


class _UnavailableKeyCipher:
    async def encrypt(self, plaintext: str) -> object:
        raise RuntimeError("kms unavailable")


@pytest.mark.asyncio
async def test_readiness_distinguishes_required_failure_from_safe_degradation() -> None:
    report = await HealthService().report(
        database=DependencyStatus.DOWN,
        migrations=DependencyStatus.UP,
        erasure_replay=DependencyStatus.UP,
        key_wrapping=DependencyStatus.UP,
        redis=DependencyStatus.DOWN,
        claude=DependencyStatus.DEGRADED,
        drive=DependencyStatus.UP,
        gmail=DependencyStatus.UP,
    )

    assert report.ready is False
    assert report.dependencies["database"].required is True
    assert report.dependencies["redis"].recoverable_from_postgres is True
    assert report.dependencies["redis"].affected_features == ("live notifications",)
    assert report.dependencies["claude"].affected_features == ("AI answers", "email drafting")


@pytest.mark.asyncio
async def test_optional_dependency_failure_keeps_process_ready_but_degraded() -> None:
    report = await HealthService().report(
        database=DependencyStatus.UP,
        migrations=DependencyStatus.UP,
        erasure_replay=DependencyStatus.UP,
        key_wrapping=DependencyStatus.UP,
        redis=DependencyStatus.DOWN,
        claude=DependencyStatus.DEGRADED,
        drive=DependencyStatus.DOWN,
        gmail=DependencyStatus.DOWN,
    )

    assert report.ready is True
    assert report.status == "degraded"
    assert report.dependencies["drive"].required is False


@pytest.mark.asyncio
@pytest.mark.parametrize("required_name", ["migrations", "erasure_replay", "key_wrapping"])
async def test_each_required_safety_gate_fails_readiness(required_name: str) -> None:
    statuses = {
        "database": DependencyStatus.UP,
        "migrations": DependencyStatus.UP,
        "erasure_replay": DependencyStatus.UP,
        "key_wrapping": DependencyStatus.UP,
        "redis": DependencyStatus.UP,
        "claude": DependencyStatus.UP,
        "drive": DependencyStatus.UP,
        "gmail": DependencyStatus.UP,
    }
    statuses[required_name] = DependencyStatus.DOWN

    report = await HealthService().report(**statuses)

    assert report.ready is False
    assert report.dependencies[required_name].required is True


@pytest.mark.asyncio
async def test_health_endpoints_keep_liveness_process_only_and_use_ready_status() -> None:
    app = create_app(Settings())

    async def report():  # type: ignore[no-untyped-def]
        return await HealthService().report(
            database=DependencyStatus.DOWN,
            migrations=DependencyStatus.DOWN,
            erasure_replay=DependencyStatus.DOWN,
            key_wrapping=DependencyStatus.DOWN,
            redis=DependencyStatus.DOWN,
            claude=DependencyStatus.DOWN,
            drive=DependencyStatus.DOWN,
            gmail=DependencyStatus.DOWN,
        )

    app.state.health_reporter = report
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert ready.json()["ready"] is False
    assert "database" in ready.json()["dependencies"]


@pytest.mark.asyncio
async def test_metrics_endpoint_exports_the_operational_contract() -> None:
    app = create_app(Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for metric in (
        "platform_http_request_latency_seconds",
        "platform_rag_retrieval_latency_seconds",
        "platform_rag_model_latency_seconds",
        "platform_job_backlog",
        "platform_expired_job_leases",
        "platform_connector_staleness_seconds",
        "platform_support_handoff_backlog",
        "platform_email_delivery_unknown",
        "platform_erasure_backlog",
    ):
        assert metric in response.text


@pytest.mark.asyncio
async def test_reachable_database_with_missing_migrations_is_reported_precisely() -> None:
    reporter = ConfiguredHealthReporter(
        Settings.model_validate({"DATABASE_URL": "postgresql+asyncpg://db.test/platform"}),
        lambda: _SessionContext(_MigrationMissingSession()),  # type: ignore[arg-type]
        redis_client=None,
    )

    database, migrations, erasure = await reporter._database_statuses()

    assert database is DependencyStatus.UP
    assert migrations is DependencyStatus.DOWN
    assert erasure is DependencyStatus.DOWN


@pytest.mark.asyncio
async def test_configured_but_unavailable_kms_fails_key_wrapping_readiness() -> None:
    reporter = ConfiguredHealthReporter(
        Settings.model_validate(
            {"GOOGLE_KMS_KEY_NAME": "projects/p/locations/l/keyRings/r/cryptoKeys/k"}
        ),
        lambda: _SessionContext(_MigrationMissingSession()),  # type: ignore[arg-type]
        redis_client=None,
        key_cipher=_UnavailableKeyCipher(),  # type: ignore[arg-type]
    )

    assert await reporter._key_wrapping_status() is DependencyStatus.DOWN
