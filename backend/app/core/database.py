from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.ext.asyncio import (
    async_sessionmaker as create_async_sessionmaker,
)

from app.core.config import Settings

settings = Settings()
if settings.database_url is None:
    raise RuntimeError("DATABASE_URL is required to configure the database")

engine = create_async_engine(settings.database_url.unicode_string(), pool_pre_ping=True)
async_sessionmaker = create_async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
