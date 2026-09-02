from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.core.celery import create_celery
from app.core.config import Settings
from app.core.logging import configure_logging
from app.modules.chat.rate_limit import SlidingWindowRateLimiter
from app.modules.chat.router import router as chat_router
from app.modules.connectors.router import router as connectors_router
from app.modules.connectors.service import ConnectorService
from app.modules.email.gmail_gateway import GoogleGmailGatewayFactory
from app.modules.email.router import router as email_router
from app.modules.identity.oidc import configure_google_oidc
from app.modules.identity.router import router as identity_router
from app.modules.knowledge.drive_gateway import GoogleDriveGatewayFactory
from app.modules.knowledge.router import router as knowledge_router
from app.modules.rag.answer_service import GroundedAnswerService
from app.modules.rag.router import router as rag_router
from app.modules.support.router import public_router as support_public_router
from app.modules.support.router import staff_router as support_staff_router


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or Settings()
    app = FastAPI(title="Enterprise AI Agent Platform", version="0.1.0")
    app.state.settings = settings
    app.state.celery = create_celery(settings)
    app.state.google_oidc_client = None
    app.state.connector_service = ConnectorService.from_settings(settings)
    app.state.drive_gateway_factory = GoogleDriveGatewayFactory.from_settings(settings)
    app.state.gmail_gateway_factory = GoogleGmailGatewayFactory.from_settings(settings)
    app.state.chat_rate_limiter = None
    app.state.chat_sse_redis = None
    if settings.redis_url is not None:
        from redis.asyncio import Redis

        chat_redis = Redis.from_url(settings.redis_url.unicode_string(), decode_responses=True)
        app.state.chat_rate_limiter = SlidingWindowRateLimiter(chat_redis)
        app.state.chat_sse_redis = chat_redis
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
    app.include_router(connectors_router)
    app.include_router(knowledge_router)
    app.include_router(rag_router)
    app.include_router(chat_router)
    app.include_router(support_public_router)
    app.include_router(support_staff_router)
    app.include_router(email_router)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app
