import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.email.classification import ClassificationExecution, EmailClassifier
from app.modules.email.gmail_gateway import (
    GmailAuthorizationError,
    GmailGateway,
    GmailGatewayFactory,
    GmailMessage,
)
from app.modules.email.models import (
    EmailAction,
    EmailCategory,
    EmailState,
    EmailStateHistory,
    EmailSyncState,
    EmailWorkItem,
)
from app.modules.email.state_machine import transition
from app.modules.jobs.models import JobIntent
from app.modules.jobs.service import JobService
from app.modules.outbox.service import OutboxService


@dataclass(frozen=True, slots=True)
class EmailIngestionResult:
    connector_id: UUID
    history_id: str | None
    ingested: int
    next_page_token: str | None
    reauth_required: bool = False


class EmailIngestionService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        classifier: EmailClassifier,
        gateway: GmailGateway | None = None,
        connector_service: ConnectorService | None = None,
        gateway_factory: GmailGatewayFactory | None = None,
        job_service: JobService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._classifier = classifier
        self._gateway = gateway
        self._connector_service = connector_service
        self._gateway_factory = gateway_factory
        self._job_service = job_service or JobService()
        self._outbox_service = outbox_service or OutboxService()

    async def ingest_message(
        self,
        message: GmailMessage,
        *,
        organization_id: UUID,
        connector_id: UUID,
        knowledge_base_id: UUID,
    ) -> EmailWorkItem:
        values = {
            "organization_id": organization_id,
            "connector_id": connector_id,
            "knowledge_base_id": knowledge_base_id,
            "gmail_message_id": message.id,
            "gmail_thread_id": _one_line(message.thread_id),
            "gmail_history_id": message.history_id,
            "sender": _normalized_address(message.sender),
            "recipients": [_normalized_address(value) for value in message.recipients],
            "subject": _one_line(message.subject),
            "body": _normalized_body(message.body),
            "received_at": message.received_at,
            "raw_content_ref": _safe_raw_reference(message.raw_content_ref, message.id),
            "state": EmailState.INGESTED,
        }
        inserted_id = await self._db_session.scalar(
            insert(EmailWorkItem)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[EmailWorkItem.organization_id, EmailWorkItem.gmail_message_id]
            )
            .returning(EmailWorkItem.id)
        )
        item = await self._db_session.scalar(
            select(EmailWorkItem).where(
                EmailWorkItem.id == inserted_id
                if inserted_id is not None
                else (
                    (EmailWorkItem.organization_id == organization_id)
                    & (EmailWorkItem.gmail_message_id == message.id)
                )
            )
        )
        if item is None:
            raise RuntimeError("email ingestion did not return the inserted or existing item")
        if inserted_id is None:
            return item
        await self._classify(item, retrying=False)
        await self._db_session.flush()
        return item

    async def ingest_history(
        self,
        connector_id: UUID,
        knowledge_base_id: UUID,
        *,
        commit: bool = True,
    ) -> EmailIngestionResult:
        connector = await self._db_session.scalar(
            select(Connector).where(Connector.id == connector_id).with_for_update()
        )
        if connector is None or connector.kind is not ConnectorKind.GMAIL:
            raise LookupError("Gmail connector not found")
        if connector.status is not ConnectorStatus.ACTIVE:
            return EmailIngestionResult(connector.id, None, 0, None, reauth_required=True)
        sync_state = await self._sync_state(connector)
        gateway = await self._resolve_gateway(connector)
        try:
            page = await gateway.list_history(sync_state.history_id, sync_state.pending_page_token)
            ingested = 0
            for message_id in page.message_ids:
                message = await gateway.get_message(message_id)
                before = await self._db_session.scalar(
                    select(EmailWorkItem.id).where(
                        EmailWorkItem.organization_id == connector.organization_id,
                        EmailWorkItem.gmail_message_id == message.id,
                    )
                )
                await self.ingest_message(
                    message,
                    organization_id=connector.organization_id,
                    connector_id=connector.id,
                    knowledge_base_id=knowledge_base_id,
                )
                ingested += int(before is None)
            sync_state.pending_page_token = page.next_page_token
            if sync_state.history_id is None or page.next_page_token is None:
                sync_state.history_id = page.history_id
            sync_state.last_error_code = None
            sync_state.last_success_at = datetime.now(UTC)
            if commit:
                await self._db_session.commit()
            else:
                await self._db_session.flush()
            return EmailIngestionResult(
                connector.id,
                sync_state.history_id,
                ingested,
                sync_state.pending_page_token,
            )
        except GmailAuthorizationError:
            await self._db_session.rollback()
            return await self._mark_reauth_required(connector_id, commit=commit)
        except Exception:
            await self._db_session.rollback()
            raise

    async def process_classification(self, work_item_id: UUID) -> EmailWorkItem:
        item = await self._db_session.scalar(
            select(EmailWorkItem).where(EmailWorkItem.id == work_item_id).with_for_update()
        )
        if item is None:
            raise LookupError("email work item not found")
        if item.category is not None:
            return item
        if item.state is EmailState.DRAFT_RETRY_WAIT:
            self._change_state(item, EmailAction.RETRY_DRAFT, reason_code="AUTOMATIC_RETRY")
        if item.state is not EmailState.DRAFTING:
            raise ValueError("email classification is not retryable in its current state")
        await self._classify(item, retrying=True)
        return item

    async def retry_failed_work_item(self, work_item_id: UUID) -> EmailWorkItem:
        item = await self._db_session.scalar(
            select(EmailWorkItem).where(EmailWorkItem.id == work_item_id).with_for_update()
        )
        if item is None:
            raise LookupError("email work item not found")
        self._change_state(item, EmailAction.RETRY_DRAFT, reason_code="MANUAL_RETRY")
        item.last_error_code = None
        kind = "email.classify" if item.category is None else "email.draft"
        phase = "classification" if item.category is None else "draft"
        await self._enqueue_work(item, kind=kind, phase=phase)
        await self._db_session.flush()
        return item

    async def _classify(self, item: EmailWorkItem, *, retrying: bool) -> None:
        try:
            execution = await self._classifier.classify(item.subject, item.body)
        except Exception:
            action = EmailAction.DRAFT_FAILED if retrying else EmailAction.CLASSIFICATION_FAILED
            self._change_state(item, action, reason_code="EMAIL_CLASSIFICATION_FAILED")
            item.last_error_code = "EMAIL_CLASSIFICATION_FAILED"
            await self._enqueue_work(item, kind="email.classify", phase="classification")
            return
        self._apply_classification(item, execution)
        if execution.classification.category in {
            EmailCategory.ACTION_REQUIRED,
            EmailCategory.UNKNOWN,
        }:
            if item.state is EmailState.INGESTED:
                self._change_state(item, EmailAction.START_DRAFT)
            await self._enqueue_work(item, kind="email.draft", phase="draft")
        elif item.state is EmailState.DRAFTING:
            self._change_state(item, EmailAction.CLASSIFIED_NO_DRAFT)

    @staticmethod
    def _apply_classification(item: EmailWorkItem, execution: ClassificationExecution) -> None:
        result = execution.classification
        item.category = result.category
        item.priority = result.priority
        item.reply_required = result.reply_required
        item.last_error_code = None
        item.classification_provenance = {
            "model": execution.model,
            "prompt_version": execution.prompt_version,
            "latency_ms": execution.latency_ms,
            "input_tokens": execution.input_tokens,
            "output_tokens": execution.output_tokens,
            "estimated_cost": execution.estimated_cost,
        }

    async def _enqueue_work(self, item: EmailWorkItem, *, kind: str, phase: str) -> JobIntent:
        payload: dict[str, object] = {
            "work_item_id": str(item.id),
            "organization_id": str(item.organization_id),
            "connector_id": str(item.connector_id),
            "knowledge_base_id": str(item.knowledge_base_id),
            "phase": phase,
        }
        job = await self._job_service.enqueue(
            self._db_session,
            kind,
            f"{kind}:{item.id}:v{item.version}",
            payload,
        )
        await self._outbox_service.add(
            self._db_session,
            f"{kind}.requested",
            "job",
            job.id,
            {
                "work_item_id": str(item.id),
                "organization_id": str(item.organization_id),
            },
        )
        return job

    def _change_state(
        self,
        item: EmailWorkItem,
        action: EmailAction,
        *,
        reason_code: str | None = None,
    ) -> None:
        before = item.state
        after = transition(before, action)
        item.state = after
        item.version += 1
        self._db_session.add(
            EmailStateHistory(
                work_item_id=item.id,
                organization_id=item.organization_id,
                from_state=before,
                to_state=after,
                action=action,
                reason_code=reason_code,
                resource_version=item.version,
            )
        )

    async def _sync_state(self, connector: Connector) -> EmailSyncState:
        state = await self._db_session.scalar(
            select(EmailSyncState)
            .where(EmailSyncState.connector_id == connector.id)
            .with_for_update()
        )
        if state is None:
            state = EmailSyncState(
                organization_id=connector.organization_id, connector_id=connector.id
            )
            self._db_session.add(state)
            await self._db_session.flush()
        return state

    async def _resolve_gateway(self, connector: Connector) -> GmailGateway:
        if self._gateway is not None:
            return self._gateway
        if self._connector_service is None or self._gateway_factory is None:
            raise RuntimeError("encrypted Gmail connector and gateway are required")
        refresh_token = await self._connector_service.load_refresh_token(
            self._db_session, connector
        )
        return (await self._gateway_factory.create(refresh_token=refresh_token)).gateway

    async def _mark_reauth_required(
        self, connector_id: UUID, *, commit: bool
    ) -> EmailIngestionResult:
        connector = await self._db_session.scalar(
            select(Connector).where(Connector.id == connector_id).with_for_update()
        )
        if connector is None:
            raise LookupError("Gmail connector not found")
        connector.status = ConnectorStatus.REAUTH_REQUIRED
        sync_state = await self._sync_state(connector)
        sync_state.last_error_code = "GMAIL_REAUTH_REQUIRED"
        await self._outbox_service.add(
            self._db_session,
            "connector.reauthorization_required",
            "connector",
            connector.id,
            {
                "organization_id": str(connector.organization_id),
                "kind": ConnectorKind.GMAIL.value,
                "error_code": "GMAIL_REAUTH_REQUIRED",
            },
        )
        if commit:
            await self._db_session.commit()
        else:
            await self._db_session.flush()
        return EmailIngestionResult(
            connector.id,
            sync_state.history_id,
            0,
            sync_state.pending_page_token,
            reauth_required=True,
        )


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _normalized_body(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalized_address(value: str) -> str:
    addresses = getaddresses([value])
    normalized = []
    for name, address in addresses:
        if not address:
            continue
        lower = address.strip().lower()
        normalized.append(f"{_one_line(name)} <{lower}>" if name.strip() else lower)
    return ", ".join(normalized) if normalized else _one_line(value)


def _safe_raw_reference(value: str, message_id: str) -> str:
    expected = f"gmail://users/me/messages/{message_id}"
    if re.fullmatch(r"gmail://[A-Za-z0-9_./:-]+", value):
        return value
    return expected
