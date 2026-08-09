from fastapi import FastAPI

from app.core.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Enterprise AI Agent Platform", version="0.1.0")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app
