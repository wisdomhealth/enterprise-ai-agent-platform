from collections.abc import Collection, Mapping
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.idempotency.models import IdempotencyRecord, IdempotencyState


class IdempotencyConflict(Exception):
    pass


class IdempotencyInProgress(Exception):
    pass


class IdempotencyLeaseLost(Exception):
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
        database_now = await self._db_session.scalar(select(func.current_timestamp()))
        if database_now is None:
            raise RuntimeError("database current timestamp is unavailable")
        lease_expires_at = database_now + timedelta(seconds=self._lease_seconds)
        lease_token = uuid4()
        record_id = await self._db_session.scalar(
            insert(IdempotencyRecord)
            .values(
                scope_id=scope_id,
                actor_id=actor_id,
                operation=operation,
                object_id=object_id,
                key=key,
                request_hash=request_hash,
                lease_token=lease_token,
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
        if existing_record.lease_expires_at > database_now:
            raise IdempotencyInProgress(key)

        existing_record.lease_token = lease_token
        existing_record.lease_expires_at = lease_expires_at
        existing_record.updated_at = database_now
        await self._db_session.flush()
        return existing_record

    async def complete(
        self,
        record_id: UUID,
        status_code: int,
        response_body: Mapping[str, object],
        *,
        lease_token: UUID,
        safe_response_keys: Collection[str] | None = None,
    ) -> IdempotencyRecord:
        if safe_response_keys is None:
            safe_response: dict[str, object] = {}
        else:
            safe_response = {
                key: response_body[key] for key in safe_response_keys if key in response_body
            }
        completed = await self._db_session.scalar(
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.id == record_id,
                IdempotencyRecord.state == IdempotencyState.IN_PROGRESS,
                IdempotencyRecord.lease_token == lease_token,
                IdempotencyRecord.lease_expires_at > func.current_timestamp(),
            )
            .values(
                state=IdempotencyState.COMPLETED,
                status_code=status_code,
                response_body=safe_response,
                updated_at=func.current_timestamp(),
            )
            .returning(IdempotencyRecord)
        )
        if completed is not None:
            return completed

        existing = await self._db_session.get(IdempotencyRecord, record_id)
        if existing is None:
            raise LookupError(record_id)
        if (
            existing.state is IdempotencyState.COMPLETED
            and existing.lease_token == lease_token
        ):
            return existing
        raise IdempotencyLeaseLost(record_id)
