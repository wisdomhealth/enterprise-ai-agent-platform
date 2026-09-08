from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker as create_async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings

settings = Settings()
if settings.database_url is None:
    raise RuntimeError("DATABASE_URL is required to configure the database")


@dataclass(frozen=True)
class ConnectionBudget:
    """Worst-case PostgreSQL connections reserved by this deployment."""

    fastapi_connections: int
    celery_connections: int
    admin_migration_reserve: int
    postgres_max_connections: int

    @property
    def required_connections(self) -> int:
        return (
            self.fastapi_connections
            + self.celery_connections
            + self.admin_migration_reserve
        )

    def assert_within_limit(self) -> None:
        if self.required_connections > self.postgres_max_connections:
            raise RuntimeError(
                "PostgreSQL connection budget exceeds POSTGRES_MAX_CONNECTIONS: "
                f"required={self.required_connections} limit={self.postgres_max_connections}"
            )


def connection_budget(config: Settings) -> ConnectionBudget:
    """Calculate the bounded FastAPI pool plus prefork worker connection budget."""
    return ConnectionBudget(
        fastapi_connections=config.api_process_count
        * (config.database_pool_size + config.database_max_overflow),
        # NullPool returns connections before asyncio.run closes its event loop.
        # Email history processing can hold a work session plus a lease-renewal
        # session, so production capacity reserves the configured peak per task.
        celery_connections=(
            config.celery_worker_instances
            * config.celery_worker_concurrency
            * config.celery_task_connection_peak
        ),
        admin_migration_reserve=config.postgres_admin_migration_reserve,
        postgres_max_connections=config.postgres_max_connections,
    )


connection_budget(settings).assert_within_limit()

# FastAPI owns the only long-lived async pool.  Its capacity is explicit so a
# deployment can budget it alongside prefork workers and migration/admin work.
engine = create_async_engine(
    settings.database_url.unicode_string(),
    pool_pre_ping=True,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
)
async_sessionmaker = create_async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Celery synchronous tasks call asyncio.run(), creating a fresh event loop per
# invocation.  A normal async pool would retain asyncpg connections bound to a
# prior loop (and potentially inherited by prefork children).  NullPool closes
# every connection when its session ends, preventing both cross-loop reuse and
# idle pools multiplied by worker processes.
celery_engine = create_async_engine(
    settings.database_url.unicode_string(),
    pool_pre_ping=True,
    poolclass=NullPool,
)
celery_async_sessionmaker = create_async_sessionmaker(
    celery_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
