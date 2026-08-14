from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.idempotency.models import IdempotencyRecord, IdempotencyState


class IdempotencyConflict(Exception):
    pass


class IdempotencyInProgress(Exception):
    pass


class IdempotencyService:
    def __init__(self, db_session: AsyncSession, *, lease_seconds: int = 60) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._db_session = db_session
        self._lease_seconds = lease_seconds

    async def begin(
        self,
        *,
        scope_id: UUID,
        actor_id: UUID,
        operation: str,
        object_id: UUID,
        key: str,
        request_hash: str,
    ) -> IdempotencyRecord:
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=self._lease_seconds)
        record_id = await self._db_session.scalar(
            insert(IdempotencyRecord)
            .values(
                scope_id=scope_id,
                actor_id=actor_id,
                operation=operation,
                object_id=object_id,
                key=key,
                request_hash=request_hash,
                lease_expires_at=lease_expires_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    IdempotencyRecord.scope_id,
                    IdempotencyRecord.actor_id,
                    IdempotencyRecord.key,
                ]
            )
            .returning(IdempotencyRecord.id)
        )
        if record_id is not None:
            inserted_record: IdempotencyRecord | None = await self._db_session.get(
                IdempotencyRecord, record_id
            )
            if inserted_record is None:
                raise RuntimeError("inserted idempotency record could not be loaded")
            return inserted_record

        existing_record: IdempotencyRecord | None = await self._db_session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.scope_id == scope_id,
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.key == key,
            )
            .with_for_update()
        )
        if existing_record is None:
            raise RuntimeError("conflicting idempotency record could not be loaded")
        if (
            existing_record.operation != operation
            or existing_record.object_id != object_id
            or existing_record.request_hash != request_hash
        ):
            raise IdempotencyConflict(key)
        if existing_record.state is IdempotencyState.COMPLETED:
            return existing_record
        if existing_record.lease_expires_at > now:
            raise IdempotencyInProgress(key)

        existing_record.lease_expires_at = lease_expires_at
        existing_record.updated_at = now
        await self._db_session.flush()
        return existing_record

    async def complete(
        self,
        record_id: UUID,
        status_code: int,
        response_body: Mapping[str, object],
        *,
        safe_response_keys: Collection[str] | None = None,
    ) -> IdempotencyRecord:
        record = await self._db_session.scalar(
            select(IdempotencyRecord)
            .where(IdempotencyRecord.id == record_id)
            .with_for_update()
        )
        if record is None:
            raise LookupError(record_id)
        if record.state is IdempotencyState.COMPLETED:
            return record

        if safe_response_keys is None:
            safe_response = dict(response_body)
        else:
            safe_response = {
                key: response_body[key] for key in safe_response_keys if key in response_body
            }
        record.state = IdempotencyState.COMPLETED
        record.status_code = status_code
        record.response_body = safe_response
        record.updated_at = datetime.now(UTC)
        await self._db_session.flush()
        return record
