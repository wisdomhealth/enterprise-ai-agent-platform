import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import Settings
from app.db.base import Base
from app.modules.authorization import models as authorization_models  # noqa: F401
from app.modules.audit import models as audit_models  # noqa: F401
from app.modules.connectors import models as connector_models  # noqa: F401
from app.modules.identity import models  # noqa: F401
from app.modules.idempotency import models as idempotency_models  # noqa: F401
from app.modules.jobs import models as job_models  # noqa: F401
from app.modules.knowledge import models as knowledge_models  # noqa: F401
from app.modules.outbox import models as outbox_models  # noqa: F401
from app.modules.rag import evaluation_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
database_url = settings.migration_database_url or settings.database_url
if database_url is not None:
    config.set_main_option(
        "sqlalchemy.url",
        database_url.unicode_string().replace("%", "%%"),
    )

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
