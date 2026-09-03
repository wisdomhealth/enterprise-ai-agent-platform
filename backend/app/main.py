from time import monotonic

from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.core.celery import create_celery
from app.core.config import Settings
from app.core.database import async_sessionmaker
from app.core.logging import configure_logging
from app.core.telemetry import (
    DEPENDENCY_UP,
    HTTP_REQUEST_LATENCY_SECONDS,
    prometheus_payload,
    record_http_request,
    refresh_operational_metrics,
)
from app.modules.chat.rate_limit import SlidingWindowRateLimiter
from app.modules.chat.router import router as chat_router
from app.modules.connectors.encryption import envelope_cipher_from_settings
from app.modules.connectors.router import router as connectors_router
from app.modules.connectors.service import ConnectorService
from app.modules.email.gmail_gateway import GoogleGmailGatewayFactory
from app.modules.email.router import router as email_router
from app.modules.identity.oidc import configure_google_oidc
from app.modules.identity.router import admin_router as identity_admin_router
from app.modules.identity.router import router as identity_router
from app.modules.knowledge.drive_gateway import GoogleDriveGatewayFactory
from app.modules.knowledge.router import router as knowledge_router
from app.modules.operations.health import ConfiguredHealthReporter, HealthReport
from app.modules.operations.router import router as operations_router
from app.modules.rag.answer_service import GroundedAnswerService
from app.modules.rag.router import router as rag_router
from app.modules.retention.router import router as retention_router
from app.modules.support.router import public_router as support_public_router
from app.modules.support.router import staff_router as support_staff_router
from app.modules.webhooks.router import router as webhooks_router


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or Settings()
    app = FastAPI(title="Enterprise AI Agent Platform", version="0.1.0")
    app.state.settings = settings
    app.state.celery = create_celery(settings)
    app.state.google_oidc_client = None
    envelope_cipher = envelope_cipher_from_settings(settings)
    app.state.connector_service = (
        ConnectorService(envelope_cipher) if envelope_cipher is not None else None
    )
    app.state.webhook_cipher = envelope_cipher
    app.state.drive_gateway_factory = GoogleDriveGatewayFactory.from_settings(settings)
    app.state.gmail_gateway_factory = GoogleGmailGatewayFactory.from_settings(settings)
    app.state.chat_rate_limiter = None
    app.state.chat_sse_redis = None
    if settings.redis_url is not None:
        from redis.asyncio import Redis

        chat_redis = Redis.from_url(settings.redis_url.unicode_string(), decode_responses=True)
        app.state.chat_rate_limiter = SlidingWindowRateLimiter(chat_redis)
        app.state.chat_sse_redis = chat_redis
    app.state.health_reporter = ConfiguredHealthReporter(
        settings,
        async_sessionmaker,
        redis_client=app.state.chat_sse_redis,
        key_cipher=envelope_cipher,
    )
    app.state.grounded_answer_service = (
        GroundedAnswerService.from_settings(settings)
        if (
            settings.openai_api_key is not None
            and settings.anthropic_api_key is not None
            and settings.redis_url is not None
        )
        else None
    )

    if settings.session_secret is not None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret.get_secret_value(),
            session_cookie="oidc_flow",
            max_age=600,
            same_site="lax",
            https_only=True,
        )

    if (
        settings.google_oidc_client_id is not None
        and settings.google_oidc_client_secret is not None
    ):
        app.state.google_oidc_client = configure_google_oidc(
            OAuth(),
            client_id=settings.google_oidc_client_id.get_secret_value(),
            client_secret=settings.google_oidc_client_secret.get_secret_value(),
        )

    app.include_router(identity_router)
    app.include_router(identity_admin_router)
    app.include_router(connectors_router)
    app.include_router(knowledge_router)
    app.include_router(rag_router)
    app.include_router(chat_router)
    app.include_router(support_public_router)
    app.include_router(support_staff_router)
    app.include_router(email_router)
    app.include_router(operations_router)
    app.include_router(retention_router)
    app.include_router(webhooks_router)

    @app.middleware("http")
    async def observe_http(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = monotonic()
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            record_http_request(request.method, route_path, status_code)
            HTTP_REQUEST_LATENCY_SECONDS.labels(method=request.method, path=route_path).observe(
                monotonic() - started
            )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> Response:
        reporter = getattr(request.app.state, "health_reporter", None)
        if reporter is None:
            report = HealthReport(ready=False, status="not_ready", dependencies={})
        else:
            report = await reporter()
        _record_dependency_health(report)
        return JSONResponse(
            report.as_dict(),
            status_code=status.HTTP_200_OK if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        reporter = getattr(request.app.state, "health_reporter", None)
        report = await reporter() if reporter is not None else None
        if report is not None:
            _record_dependency_health(report)
        try:
            async with async_sessionmaker() as session:
                await refresh_operational_metrics(
                    session,
                    restore_generation=request.app.state.settings.restore_generation,
                )
            DEPENDENCY_UP.labels(dependency="database").set(1)
        except Exception:
            DEPENDENCY_UP.labels(dependency="database").set(0)
        payload, content_type = prometheus_payload()
        return Response(content=payload, media_type=content_type)

    return app


def _record_dependency_health(report: HealthReport) -> None:
    for name, dependency in report.dependencies.items():
        DEPENDENCY_UP.labels(dependency=name).set(1 if dependency.status.value == "up" else 0)
