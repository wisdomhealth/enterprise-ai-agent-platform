import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.email.actors import EMAIL_SYSTEM_ACTOR_TYPE, email_worker_actor_id
from app.modules.email.classification import (
    ClassificationExecution,
    EmailClassificationFailure,
    EmailClassifier,
    classify_email_error,
)
from app.modules.email.gmail_gateway import (
    GmailAuthorizationError,
    GmailGateway,
    GmailGatewayFactory,
    GmailHistoryCursorExpired,
    GmailMessage,
    GmailPageTokenInvalid,
)
from app.modules.email.models import (
    EmailAction,
    EmailCategory,
    EmailState,
    EmailStateHistory,
    EmailSyncState,
    EmailWorkItem,
    GmailSyncStatus,
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
        classification_retry_after_cap_seconds: int = 300,
        draft_max_attempts: int = 3,
        recovery_lookback_days: int = 7,
        recovery_max_messages: int = 500,
    ) -> None:
        self._db_session = db_session
        self._classifier = classifier
        self._gateway = gateway
        self._connector_service = connector_service
        self._gateway_factory = gateway_factory
        self._job_service = job_service or JobService()
        self._outbox_service = outbox_service or OutboxService()
        self._classification_retry_after_cap_seconds = classification_retry_after_cap_seconds
        self._draft_max_attempts = draft_max_attempts
        self._recovery_lookback_days = recovery_lookback_days
        self._recovery_max_messages = recovery_max_messages

    async def ingest_message(
        self,
        message: GmailMessage,
        *,
        organization_id: UUID,
        connector_id: UUID,
        knowledge_base_id: UUID,
        job_id: UUID | None = None,
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
        await self._enqueue_work(item, kind="email.classify", phase="classification")
        await self._db_session.flush()
        return item

    async def ingest_history(
        self,
        connector_id: UUID,
        knowledge_base_id: UUID,
        *,
        commit: bool = True,
        job_id: UUID | None = None,
    ) -> EmailIngestionResult:
        connector = await self._db_session.scalar(
            select(Connector).where(Connector.id == connector_id)
        )
        if connector is None or connector.kind is not ConnectorKind.GMAIL:
            raise LookupError("Gmail connector not found")
        if connector.status is not ConnectorStatus.ACTIVE:
            return EmailIngestionResult(connector.id, None, 0, None, reauth_required=True)
        sync_state = await self._sync_state(connector)
        await self._db_session.commit()
        gateway = await self._resolve_gateway(connector)
        try:
            if sync_state.processed_history_id is None:
                return await self._initialize_history(
                    connector,
                    sync_state,
                    gateway,
                    knowledge_base_id,
                    job_id=job_id,
                )
            page_token = sync_state.pending_page_token
            start_history_id = (
                sync_state.sync_start_history_id
                if page_token is not None and sync_state.sync_start_history_id is not None
                else sync_state.processed_history_id
            )
            if page_token is None:
                sync_state.sync_start_history_id = start_history_id
                sync_state.page_token_generation = sync_state.sync_generation
            ingested = 0
            while True:
                try:
                    page = await gateway.list_history(start_history_id, page_token)
                except GmailPageTokenInvalid:
                    sync_state = await self._locked_sync_state(connector.id)
                    sync_state.pending_page_token = None
                    page_token = None
                    await self._db_session.commit()
                    continue
                except GmailHistoryCursorExpired:
                    sync_state = await self._locked_sync_state(connector.id)
                    sync_state.processed_history_id = None
                    sync_state.pending_page_token = None
                    sync_state.sync_start_history_id = None
                    sync_state.page_token_generation = None
                    sync_state.sync_status = GmailSyncStatus.RECOVERY_REQUIRED
                    await self._db_session.commit()
                    return await self._initialize_history(
                        connector,
                        sync_state,
                        gateway,
                        knowledge_base_id,
                        job_id=job_id,
                    )
                messages = [
                    await gateway.get_message(message_id) for message_id in page.message_ids
                ]
                sync_state = await self._locked_sync_state(connector.id)
                for message in messages:
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
                        job_id=job_id,
                    )
                    ingested += int(before is None)
                sync_state.pending_page_token = page.next_page_token
                if page.next_page_token is None:
                    sync_state.processed_history_id = page.history_id
                    sync_state.sync_start_history_id = None
                    sync_state.page_token_generation = None
                await self._db_session.commit()
                if page.next_page_token is None:
                    break
                page_token = page.next_page_token
            sync_state.last_error_code = None
            if commit:
                await self._db_session.commit()
            else:
                await self._db_session.flush()
            return EmailIngestionResult(
                connector.id,
                sync_state.processed_history_id,
                ingested,
                sync_state.pending_page_token,
            )
        except GmailAuthorizationError:
            await self._db_session.rollback()
            return await self._mark_reauth_required(connector_id, commit=commit)
        except Exception:
            await self._db_session.rollback()
            raise

    async def _initialize_history(
        self,
        connector: Connector,
        sync_state: EmailSyncState,
        gateway: GmailGateway,
        knowledge_base_id: UUID,
        *,
        job_id: UUID | None,
    ) -> EmailIngestionResult:
        if sync_state.recovery_baseline_history_id is None:
            profile = await gateway.get_profile()
            sync_state = await self._locked_sync_state(connector.id)
            sync_state.recovery_batch_id = uuid4()
            sync_state.recovery_baseline_history_id = profile.history_id
            sync_state.recovery_window_started_at = datetime.now(UTC) - timedelta(
                days=self._recovery_lookback_days
            )
            sync_state.recovery_started_at = datetime.now(UTC)
            sync_state.recovery_processed_count = 0
            sync_state.recovery_message_limit = self._recovery_max_messages
            sync_state.recovery_has_more = True
            sync_state.sync_status = GmailSyncStatus.RECOVERY_RUNNING
            await self._db_session.commit()
        ingested = 0
        while sync_state.recovery_has_more:
            recovery_window_started_at = sync_state.recovery_window_started_at
            if recovery_window_started_at is None:
                raise RuntimeError("Gmail recovery window is not initialized")
            remaining = (
                sync_state.recovery_message_limit - sync_state.recovery_processed_count
            )
            if remaining <= 0:
                sync_state.sync_status = GmailSyncStatus.RECOVERY_TRUNCATED
                await self._db_session.commit()
                return EmailIngestionResult(
                    connector.id, None, ingested, sync_state.recovery_page_token
                )
            recent_page = await gateway.list_recent_inbox(
                after=recovery_window_started_at,
                page_token=sync_state.recovery_page_token,
                max_results=min(100, remaining),
            )
            messages = [
                await gateway.get_message(message_id) for message_id in recent_page.message_ids
            ]
            sync_state = await self._locked_sync_state(connector.id)
            for message in messages:
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
                    job_id=job_id,
                )
                ingested += int(before is None)
            sync_state.recovery_processed_count += len(recent_page.message_ids)
            sync_state.recovery_page_token = recent_page.next_page_token
            sync_state.recovery_has_more = recent_page.next_page_token is not None
            await self._db_session.commit()
        baseline = sync_state.recovery_baseline_history_id
        page_token = None
        final_history_id = baseline
        while True:
            history_page = await gateway.list_history(baseline, page_token)
            messages = [
                await gateway.get_message(message_id) for message_id in history_page.message_ids
            ]
            sync_state = await self._locked_sync_state(connector.id)
            for message in messages:
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
                    job_id=job_id,
                )
                ingested += int(before is None)
            final_history_id = history_page.history_id
            page_token = history_page.next_page_token
            await self._db_session.commit()
            if page_token is None:
                break
        sync_state = await self._locked_sync_state(connector.id)
        sync_state.processed_history_id = final_history_id
        if (
            sync_state.pending_history_id is not None
            and int(sync_state.pending_history_id) <= int(final_history_id)
        ):
            sync_state.pending_history_id = None
        sync_state.recovery_batch_id = None
        sync_state.recovery_baseline_history_id = None
        sync_state.recovery_window_started_at = None
        sync_state.recovery_page_token = None
        sync_state.recovery_processed_count = 0
        sync_state.recovery_message_limit = 0
        sync_state.recovery_has_more = False
        sync_state.recovery_started_at = None
        sync_state.sync_status = GmailSyncStatus.IDLE
        await self._db_session.commit()
        return EmailIngestionResult(connector.id, final_history_id, ingested, None)

    async def process_classification(
        self, work_item_id: UUID, *, job_id: UUID | None = None
    ) -> EmailWorkItem:
        item = await self._db_session.get(EmailWorkItem, work_item_id)
        if item is None:
            raise LookupError("email work item not found")
        if item.category is not None:
            return item
        if item.state not in {EmailState.INGESTED, EmailState.DRAFT_RETRY_WAIT}:
            raise ValueError("email classification is not retryable in its current state")
        subject = item.subject
        body = item.body
        try:
            execution = await self._classifier.classify(subject, body)
        except Exception as error:
            failure = classify_email_error(
                error,
                retry_after_cap_seconds=self._classification_retry_after_cap_seconds,
            )
            await self._record_classification_failure(work_item_id, failure, job_id=job_id)
            raise failure from error

        item = await self._db_session.scalar(
            select(EmailWorkItem)
            .where(EmailWorkItem.id == work_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise LookupError("email work item not found")
        if item.category is not None:
            return item
        if item.state not in {EmailState.INGESTED, EmailState.DRAFT_RETRY_WAIT}:
            raise ValueError("email classification is not retryable in its current state")
        self._apply_classification(item, execution)
        if execution.classification.category in {
            EmailCategory.ACTION_REQUIRED,
            EmailCategory.UNKNOWN,
        }:
            if item.state is EmailState.INGESTED:
                self._change_state(item, EmailAction.START_DRAFT, job_id=job_id)
            else:
                self._change_state(
                    item,
                    EmailAction.RETRY_DRAFT,
                    reason_code="AUTOMATIC_RETRY",
                    job_id=job_id,
                )
            await self._enqueue_work(item, kind="email.draft", phase="draft")
        elif item.state is EmailState.DRAFT_RETRY_WAIT:
            self._change_state(
                item,
                EmailAction.RETRY_DRAFT,
                reason_code="AUTOMATIC_RETRY",
                job_id=job_id,
            )
            self._change_state(item, EmailAction.CLASSIFIED_NO_DRAFT, job_id=job_id)
        await self._db_session.flush()
        return item

    async def _record_classification_failure(
        self,
        work_item_id: UUID,
        failure: EmailClassificationFailure,
        *,
        job_id: UUID | None,
    ) -> None:
        item = await self._db_session.scalar(
            select(EmailWorkItem)
            .where(EmailWorkItem.id == work_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise LookupError("email work item not found")
        if item.category is not None:
            return
        if item.state is EmailState.INGESTED:
            self._change_state(
                item,
                EmailAction.CLASSIFICATION_FAILED,
                reason_code=failure.error_code,
                job_id=job_id,
            )
        elif item.state is not EmailState.DRAFT_RETRY_WAIT:
            raise ValueError("email classification failure is stale")
        item.last_error_code = failure.error_code
        await self._db_session.flush()

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
            f"{kind}:{item.id}"
            if kind == "email.classify"
            else f"{kind}:{item.id}:v{item.version}",
            payload,
        )
        if kind == "email.draft":
            job.attempt_cycle = 1
            job.cycle_start_attempts = job.attempts
            job.cycle_attempt_limit = self._draft_max_attempts
            item.active_draft_job_id = job.id
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
        job_id: UUID | None = None,
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
                actor_id=email_worker_actor_id(item.organization_id),
                actor_type=EMAIL_SYSTEM_ACTOR_TYPE,
                job_id=job_id,
                resource_version=item.version,
            )
        )

    async def _sync_state(self, connector: Connector) -> EmailSyncState:
        state = await self._db_session.scalar(
            select(EmailSyncState)
            .where(EmailSyncState.connector_id == connector.id)
        )
        if state is None:
            state = EmailSyncState(
                organization_id=connector.organization_id, connector_id=connector.id
            )
            self._db_session.add(state)
            await self._db_session.flush()
        return state

    async def _locked_sync_state(self, connector_id: UUID) -> EmailSyncState:
        state = await self._db_session.scalar(
            select(EmailSyncState)
            .where(EmailSyncState.connector_id == connector_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if state is None:
            raise LookupError("Gmail sync state not found")
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
            sync_state.processed_history_id,
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
