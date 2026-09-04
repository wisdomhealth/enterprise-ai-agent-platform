from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.retention.models import ErasureRequest, ErasureStatus

if TYPE_CHECKING:
    from app.modules.connectors.encryption import EnvelopeCipher


class DependencyStatus(StrEnum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    status: DependencyStatus
    required: bool
    recoverable_from_postgres: bool
    affected_features: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HealthReport:
    ready: bool
    status: str
    dependencies: Mapping[str, DependencyHealth]

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "status": self.status,
            "dependencies": {
                name: asdict(dependency) for name, dependency in self.dependencies.items()
            },
        }


class HealthService:
    _REQUIRED = frozenset({"database", "migrations", "erasure_replay", "key_wrapping"})
    _FEATURES = {
        "database": ("all durable operations",),
        "migrations": ("all durable operations",),
        "erasure_replay": ("post-restore customer access",),
        "key_wrapping": ("connector and webhook credentials",),
        "redis": ("live notifications",),
        "claude": ("AI answers", "email drafting"),
        "drive": ("knowledge synchronization",),
        "gmail": ("email synchronization and delivery",),
    }

    async def report(
        self,
        *,
        database: DependencyStatus,
        migrations: DependencyStatus,
        erasure_replay: DependencyStatus,
        key_wrapping: DependencyStatus,
        redis: DependencyStatus,
        claude: DependencyStatus,
        drive: DependencyStatus,
        gmail: DependencyStatus,
    ) -> HealthReport:
        statuses = {
            "database": database,
            "migrations": migrations,
            "erasure_replay": erasure_replay,
            "key_wrapping": key_wrapping,
            "redis": redis,
            "claude": claude,
            "drive": drive,
            "gmail": gmail,
        }
        dependencies = {
            name: DependencyHealth(
                status=value,
                required=name in self._REQUIRED,
                recoverable_from_postgres=name == "redis",
                affected_features=self._FEATURES[name],
            )
            for name, value in statuses.items()
        }
        ready = all(
            dependency.status is DependencyStatus.UP
            for dependency in dependencies.values()
            if dependency.required
        )
        degraded = any(
            dependency.status is not DependencyStatus.UP for dependency in dependencies.values()
        )
        return HealthReport(
            ready=ready,
            status="ready" if ready and not degraded else "degraded" if ready else "not_ready",
            dependencies=dependencies,
        )


type HealthReporter = Callable[[], Awaitable[HealthReport]]


class ConfiguredHealthReporter:
    """Check only safe dependencies and expose no exception or credential details."""

    EXPECTED_MIGRATION = "0021_webhook_subscriptions"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        redis_client: object | None,
        key_cipher: EnvelopeCipher | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._redis = redis_client
        self._key_cipher = key_cipher

    async def __call__(self) -> HealthReport:
        database, migrations, erasure_replay = await self._database_statuses()
        return await HealthService().report(
            database=database,
            migrations=migrations,
            erasure_replay=erasure_replay,
            key_wrapping=await self._key_wrapping_status(),
            redis=await self._redis_status(),
            claude=_configured(self._settings.anthropic_api_key is not None),
            drive=_configured(
                self._settings.google_drive_client_id is not None
                and self._settings.google_drive_client_secret is not None
            ),
            gmail=_configured(
                self._settings.google_gmail_client_id is not None
                and self._settings.google_gmail_client_secret is not None
            ),
        )

    async def _database_statuses(
        self,
    ) -> tuple[DependencyStatus, DependencyStatus, DependencyStatus]:
        if self._settings.database_url is None:
            return (DependencyStatus.DOWN,) * 3
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
                try:
                    version = await session.scalar(text("SELECT version_num FROM alembic_version"))
                except Exception:
                    return DependencyStatus.UP, DependencyStatus.DOWN, DependencyStatus.DOWN
                migrations = (
                    DependencyStatus.UP
                    if version == self.EXPECTED_MIGRATION
                    else DependencyStatus.DOWN
                )
                if migrations is DependencyStatus.DOWN:
                    return DependencyStatus.UP, migrations, DependencyStatus.DOWN
                try:
                    erasure_replay = await self._erasure_status(session)
                except Exception:
                    return DependencyStatus.UP, migrations, DependencyStatus.DOWN
                return DependencyStatus.UP, migrations, erasure_replay
        except Exception:
            return (DependencyStatus.DOWN,) * 3

    async def _erasure_status(self, session: AsyncSession) -> DependencyStatus:
        generation = self._settings.restore_generation
        if generation == 0:
            return DependencyStatus.UP
        remaining = await session.scalar(
            select(func.count(ErasureRequest.id)).where(
                or_(
                    ErasureRequest.status != ErasureStatus.APPLIED,
                    ErasureRequest.replay_generation < generation,
                )
            )
        )
        return DependencyStatus.UP if int(remaining or 0) == 0 else DependencyStatus.DOWN

    async def _redis_status(self) -> DependencyStatus:
        if self._redis is None:
            return DependencyStatus.DOWN
        ping = getattr(self._redis, "ping", None)
        if not callable(ping):
            return DependencyStatus.DOWN
        try:
            async with asyncio.timeout(1):
                return DependencyStatus.UP if await ping() else DependencyStatus.DOWN
        except Exception:
            return DependencyStatus.DOWN

    async def _key_wrapping_status(self) -> DependencyStatus:
        if self._settings.google_kms_key_name is None:
            return _key_wrapping_status(self._settings)
        if self._key_cipher is None:
            return DependencyStatus.DOWN
        try:
            async with asyncio.timeout(2):
                await self._key_cipher.encrypt("readiness-probe")
        except Exception:
            return DependencyStatus.DOWN
        return DependencyStatus.UP


def _configured(value: bool) -> DependencyStatus:
    return DependencyStatus.UP if value else DependencyStatus.DOWN


def _key_wrapping_status(settings: Settings) -> DependencyStatus:
    if settings.google_kms_key_name:
        return DependencyStatus.UP
    if settings.connector_file_key_path is None:
        return DependencyStatus.DOWN
    try:
        key = Path(settings.connector_file_key_path).read_bytes()
    except OSError:
        return DependencyStatus.DOWN
    return DependencyStatus.UP if len(key) == 32 else DependencyStatus.DOWN
