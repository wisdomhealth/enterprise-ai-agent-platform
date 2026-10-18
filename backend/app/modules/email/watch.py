from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.email.gmail_gateway import GmailGateway, GmailGatewayFactory
from app.modules.email.models import EmailSyncState, GmailWatchStatus


@dataclass(frozen=True, slots=True)
class WatchOperation:
    connector_id: UUID
    generation: int
    action: Literal["START", "STOP", "RENEW"]


class GmailWatchService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        connector_service: ConnectorService,
        gateway_factory: GmailGatewayFactory,
        topic_name: str,
    ) -> None:
        self._db_session = db_session
        self._connector_service = connector_service
        self._gateway_factory = gateway_factory
        self._topic_name = topic_name

    async def start(self, connector_id: UUID, organization_id: UUID) -> EmailSyncState:
        connector_id, connector_organization_id, gateway = await self._gateway(
            connector_id, organization_id
        )
        profile = await gateway.get_profile()
        state = await self._locked_state(connector_id, connector_organization_id)
        state.mailbox_address = profile.email_address.strip().lower()
        state.auto_sync_enabled = True
        state.watch_status = GmailWatchStatus.STARTING
        state.watch_operation_generation += 1
        state.watch_topic_name = self._topic_name
        state.watch_last_error_code = None
        operation = WatchOperation(connector_id, state.watch_operation_generation, "START")
        await self._db_session.commit()
        return await self._complete_watch(operation, gateway)

    async def renew(self, connector_id: UUID, organization_id: UUID) -> EmailSyncState:
        connector_id, connector_organization_id, gateway = await self._gateway(
            connector_id, organization_id
        )
        state = await self._locked_state(connector_id, connector_organization_id)
        if not state.auto_sync_enabled:
            raise ValueError("automatic Gmail synchronization is stopped")
        state.watch_status = GmailWatchStatus.STARTING
        state.watch_operation_generation += 1
        state.watch_last_error_code = None
        operation = WatchOperation(connector_id, state.watch_operation_generation, "RENEW")
        await self._db_session.commit()
        return await self._complete_watch(operation, gateway)

    async def stop(self, connector_id: UUID, organization_id: UUID) -> EmailSyncState:
        connector_id, connector_organization_id, gateway = await self._gateway(
            connector_id, organization_id
        )
        state = await self._locked_state(connector_id, connector_organization_id)
        state.auto_sync_enabled = False
        state.watch_status = GmailWatchStatus.STOPPING
        state.watch_operation_generation += 1
        operation = WatchOperation(connector_id, state.watch_operation_generation, "STOP")
        await self._db_session.commit()
        try:
            await gateway.stop_watch()
        except Exception:
            await self._apply_stop_failure(operation)
            raise
        return await self._apply_stop_success(operation)

    async def _complete_watch(
        self, operation: WatchOperation, gateway: GmailGateway
    ) -> EmailSyncState:
        try:
            result = await gateway.start_watch(self._topic_name)
        except Exception:
            await self._apply_watch_failure(operation)
            raise
        state = await self._lock_operation(operation)
        if state is None:
            return await self._current(operation.connector_id)
        if not state.auto_sync_enabled:
            await self._db_session.rollback()
            return state
        state.watch_status = GmailWatchStatus.ACTIVE
        state.watch_expiration_at = result.expiration
        state.watch_history_id = result.history_id
        state.last_watch_success_at = datetime.now(UTC)
        state.watch_last_error_code = None
        state.version += 1
        await self._db_session.commit()
        return state

    async def _apply_watch_failure(self, operation: WatchOperation) -> None:
        state = await self._lock_operation(operation)
        if state is not None:
            state.watch_status = GmailWatchStatus.ERROR
            state.watch_last_error_code = "GMAIL_WATCH_REGISTRATION_FAILED"
            state.version += 1
            await self._db_session.commit()

    async def _apply_stop_failure(self, operation: WatchOperation) -> None:
        state = await self._lock_operation(operation)
        if state is not None:
            state.watch_status = GmailWatchStatus.ERROR
            state.watch_last_error_code = "GMAIL_WATCH_STOP_FAILED"
            state.version += 1
            await self._db_session.commit()

    async def _apply_stop_success(self, operation: WatchOperation) -> EmailSyncState:
        state = await self._lock_operation(operation)
        if state is None:
            return await self._current(operation.connector_id)
        state.watch_status = GmailWatchStatus.STOPPED
        state.watch_expiration_at = None
        state.watch_history_id = None
        state.watch_last_error_code = None
        state.version += 1
        await self._db_session.commit()
        return state

    async def _gateway(
        self, connector_id: UUID, organization_id: UUID
    ) -> tuple[UUID, UUID, GmailGateway]:
        connector = await self._db_session.scalar(
            select(Connector).where(
                Connector.id == connector_id,
                Connector.organization_id == organization_id,
                Connector.kind == ConnectorKind.GMAIL,
                Connector.status == ConnectorStatus.ACTIVE,
            )
        )
        if connector is None:
            raise LookupError("active Gmail connector not found")
        refresh_token = await self._connector_service.load_refresh_token(
            self._db_session, connector
        )
        gateway = (await self._gateway_factory.create(refresh_token=refresh_token)).gateway
        resolved_connector_id = connector.id
        resolved_organization_id = connector.organization_id
        await self._db_session.rollback()
        return resolved_connector_id, resolved_organization_id, gateway

    async def _locked_state(
        self, connector_id: UUID, organization_id: UUID
    ) -> EmailSyncState:
        state = await self._db_session.scalar(
            select(EmailSyncState)
            .where(EmailSyncState.connector_id == connector_id)
            .with_for_update()
        )
        if state is None:
            state = EmailSyncState(
                organization_id=organization_id,
                connector_id=connector_id,
            )
            self._db_session.add(state)
            await self._db_session.flush()
        return state

    async def _lock_operation(self, operation: WatchOperation) -> EmailSyncState | None:
        return await self._db_session.scalar(
            select(EmailSyncState)
            .where(
                EmailSyncState.connector_id == operation.connector_id,
                EmailSyncState.watch_operation_generation == operation.generation,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def _current(self, connector_id: UUID) -> EmailSyncState:
        state = await self._db_session.scalar(
            select(EmailSyncState).where(EmailSyncState.connector_id == connector_id)
        )
        if state is None:
            raise LookupError("Gmail sync state not found")
        return state
