import pytest
from sqlalchemy.pool import NullPool

from app.core.celery import create_celery
from app.core.config import Settings
from app.core.database import (
    async_sessionmaker,
    celery_async_sessionmaker,
    celery_engine,
    connection_budget,
    engine,
)


def test_celery_sessions_use_an_unpooled_engine() -> None:
    """Celery tasks enter a fresh event loop for every ``asyncio.run`` call."""
    assert celery_async_sessionmaker.kw["bind"] is celery_engine
    assert isinstance(celery_engine.sync_engine.pool, NullPool)
    assert async_sessionmaker.kw["bind"] is engine
    assert not isinstance(engine.sync_engine.pool, NullPool)


def test_connection_budget_rejects_unsafe_prefork_configuration() -> None:
    settings = Settings.model_validate(
        {
            "DATABASE_URL": "postgresql+asyncpg://platform@127.0.0.1/platform",
            "DATABASE_POOL_SIZE": 5,
            "DATABASE_MAX_OVERFLOW": 3,
            "API_PROCESS_COUNT": 2,
            "CELERY_WORKER_CONCURRENCY": 4,
            "CELERY_WORKER_INSTANCES": 3,
            "CELERY_TASK_CONNECTION_PEAK": 2,
            "POSTGRES_ADMIN_MIGRATION_RESERVE": 10,
            "POSTGRES_MAX_CONNECTIONS": 60,
        }
    )

    budget = connection_budget(settings)

    assert budget.fastapi_connections == 16
    assert budget.celery_connections == 24
    assert budget.required_connections == 50
    budget.assert_within_limit()

    with pytest.raises(RuntimeError, match="connection budget exceeds"):
        budget.__class__(
            fastapi_connections=budget.fastapi_connections,
            celery_connections=budget.celery_connections,
            admin_migration_reserve=budget.admin_migration_reserve,
            postgres_max_connections=49,
        ).assert_within_limit()


def test_all_registered_celery_task_modules_use_celery_safe_sessions() -> None:
    from app.modules.chat import tasks as chat_tasks
    from app.modules.email import tasks as email_tasks
    from app.modules.knowledge import tasks as knowledge_tasks
    from app.modules.retention import tasks as retention_tasks
    from app.modules.webhooks import tasks as webhook_tasks

    for task_module in (
        chat_tasks,
        email_tasks,
        knowledge_tasks,
        retention_tasks,
        webhook_tasks,
    ):
        assert task_module.async_sessionmaker is celery_async_sessionmaker


def test_celery_concurrency_matches_the_connection_budget_configuration() -> None:
    settings = Settings.model_validate(
        {
            "DATABASE_URL": "postgresql+asyncpg://platform@127.0.0.1/platform",
            "CELERY_WORKER_CONCURRENCY": 7,
        }
    )

    assert create_celery(settings).conf.worker_concurrency == 7


def test_worker_rag_assembly_accepts_the_celery_safe_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.rag.answer_service import GroundedAnswerService
    from app.modules.rag.embeddings import OpenAIEmbeddingProvider
    from app.modules.rag.retriever import HybridRetriever

    captured: dict[str, object] = {}

    def fake_retriever_factory(
        session_factory: object,
        _embedding_provider: object,
        **_kwargs: object,
    ) -> object:
        captured["session_factory"] = session_factory
        return object()

    monkeypatch.setattr(
        HybridRetriever,
        "from_session_factory",
        fake_retriever_factory,
    )
    monkeypatch.setattr(
        OpenAIEmbeddingProvider,
        "from_settings",
        classmethod(lambda _cls, _settings: object()),
    )
    settings = Settings.model_validate(
        {
            "DATABASE_URL": "postgresql+asyncpg://platform@127.0.0.1/platform",
            "OPENAI_API_KEY": "test-openai-key",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )

    GroundedAnswerService.from_settings(settings, session_factory=celery_async_sessionmaker)

    assert captured["session_factory"] is celery_async_sessionmaker
