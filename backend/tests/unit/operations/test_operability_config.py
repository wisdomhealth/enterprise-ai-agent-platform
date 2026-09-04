from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.modules.operations.health import DependencyStatus, _key_wrapping_status

ROOT = Path(__file__).resolve().parents[4]


def test_compose_contains_the_complete_runtime_with_healthchecks() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for service in (
        "postgres",
        "redis",
        "backend",
        "worker",
        "scheduler",
        "frontend",
        "nginx",
        "prometheus",
        "alertmanager",
        "loki",
        "promtail",
        "grafana",
    ):
        assert f"  {service}:" in compose
    assert compose.count("healthcheck:") >= 11
    assert "pgvector/pgvector:pg17" in compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-}" in compose
    assert "GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-}" in compose
    assert compose.count("app.core.celery:celery_app") == 3
    assert "APP_ENV: ${APP_ENV:-production}" in compose
    assert "RERANKER_ENABLED: ${RERANKER_ENABLED:-false}" in compose


def test_nginx_tls_headers_limits_and_sse_settings_fail_safe() -> None:
    nginx = (ROOT / "infra/nginx/nginx.conf").read_text(encoding="utf-8")
    for token in (
        "listen 443 ssl",
        "client_max_body_size 10m",
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options DENY",
        "proxy_buffering off",
        "proxy_cache off",
        "X-Accel-Buffering no",
        "proxy_read_timeout 3600s",
    ):
        assert token in nginx
    assert "location ~ ^/(health|metrics)" not in nginx


def test_prometheus_alerts_cover_every_approved_failure_signal() -> None:
    alerts = (ROOT / "infra/prometheus/alerts.yml").read_text(encoding="utf-8")
    for alert in (
        "PlatformDatabaseUnavailable",
        "PlatformDriveSyncStale",
        "PlatformModelErrorsSustained",
        "PlatformExpiredJobLease",
        "PlatformSupportBacklog",
        "PlatformDeliveryUnknownOld",
    ):
        assert f"alert: {alert}" in alerts
    assert "platform_connector_staleness_seconds > 1800" in alerts
    assert "platform_email_delivery_unknown_oldest_age_seconds > 900" in alerts
    assert 'outcome=~"provider_error|validation_refusal"' in alerts
    assert 'outcome!="success"' not in alerts


def test_dashboard_and_operability_manifest_are_machine_valid() -> None:
    dashboard = json.loads(
        (ROOT / "infra/grafana/dashboards/platform-overview.json").read_text(encoding="utf-8")
    )
    assert dashboard["uid"] == "platform-overview"
    assert len(dashboard["panels"]) >= 4
    result = subprocess.run(
        [str(ROOT / "scripts/check-operability"), "--compose-file", "compose.yaml"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "PASS"
    assert [item["subsystem"] for item in report["systems"]] == [
        "S0",
        "S1",
        "S2",
        "S3",
        "S4",
        "S5",
        "S6",
        "S7",
    ]


def test_local_key_wrapping_readiness_requires_an_actual_32_byte_key(tmp_path: Path) -> None:
    key_path = tmp_path / "connector.key"
    settings = Settings.model_validate(
        {
            "APP_ENV": "development",
            "SELF_HOSTED_FILE_KEY_ALLOWED": True,
            "CONNECTOR_FILE_KEY_PATH": key_path,
        }
    )

    assert _key_wrapping_status(settings) is DependencyStatus.DOWN
    key_path.write_bytes(b"short")
    assert _key_wrapping_status(settings) is DependencyStatus.DOWN
    key_path.write_bytes(b"k" * 32)
    assert _key_wrapping_status(settings) is DependencyStatus.UP
