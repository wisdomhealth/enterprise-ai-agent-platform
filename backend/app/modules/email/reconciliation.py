from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.email.delivery import (
    EmailDeliveryAuthorizationError,
    EmailDeliveryConflict,
    EmailDeliveryResult,
)
from app.modules.email.gmail_gateway import GmailGatewayFactory, GmailSentMessage
from app.modules.email.models import (
    DeliveryIntent,
    EmailAction,
    EmailDraftVersion,
    EmailState,
    EmailStateHistory,
    EmailWorkItem,
    SuccessfulDelivery,
)
from app.modules.email.state_machine import transition
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import JobIntent, JobState
from app.modules.outbox.service import OutboxService


class GmailReconciliationGateway(Protocol):
    async def find_sent(
        self,
        *,
        deterministic_message_id: str,
        thread_id: str,
        recipients: tuple[str, ...],
        sent_after: datetime,
        sent_before: datetime,
    ) -> GmailSentMessage | None: ...


class ReconciliationService:
    def __init__(
        self,
        db_session: AsyncSession,
        gateway: GmailReconciliationGateway | None,
        principal: Principal,
        *,
        connector_service: ConnectorService | None = None,
        gateway_factory: GmailGatewayFactory | None = None,
        authorization_service: AuthorizationService | None = None,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._gateway = gateway
        self._principal = principal
        self._connector_service = connector_service
        self._gateway_factory = gateway_factory
        self._authorization = authorization_service or AuthorizationService(db_session)
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()

    async def reconcile(
        self,
        delivery_intent_id: UUID,
        *,
        confirm_absent: bool = False,
        expected_version: int | None = None,
    ) -> EmailDeliveryResult:
        intent = await self._db_session.scalar(
            select(DeliveryIntent).where(
                DeliveryIntent.id == delivery_intent_id,
                DeliveryIntent.organization_id == self._principal.organization_id,
            )
        )
        if intent is None:
            raise LookupError("email delivery intent not found")
        if expected_version is not None and intent.version != expected_version:
            raise EmailDeliveryConflict(intent)
        item = await self._db_session.get(EmailWorkItem, intent.work_item_id)
        draft = await self._db_session.get(EmailDraftVersion, intent.approved_draft_version_id)
        if item is None or draft is None or draft.work_item_id != item.id:
            raise LookupError("email delivery source is unavailable")
        await self._require_access(item)
        if intent.state is not EmailState.DELIVERY_UNKNOWN:
            raise ValueError("delivery is not awaiting reconciliation")
        expected_version = intent.version
        database_now = await self._db_session.scalar(select(func.clock_timestamp()))
        if database_now is None:
            raise RuntimeError("database clock is unavailable")
        gateway = await self._resolve_gateway(item)
        await self._db_session.commit()
        found = await gateway.find_sent(
            deterministic_message_id=intent.deterministic_message_id,
            thread_id=draft.thread_id,
            recipients=tuple([*draft.to, *draft.cc]),
            sent_after=intent.created_at - timedelta(minutes=5),
            sent_before=database_now + timedelta(minutes=5),
        )

        intent = await self._db_session.scalar(
            select(DeliveryIntent)
            .where(
                DeliveryIntent.id == intent.id,
                DeliveryIntent.state == EmailState.DELIVERY_UNKNOWN,
                DeliveryIntent.version == expected_version,
            )
            .with_for_update()
        )
        if intent is None:
            raise RuntimeError("delivery reconciliation state changed")
        item = await self._db_session.scalar(
            select(EmailWorkItem).where(EmailWorkItem.id == intent.work_item_id).with_for_update()
        )
        if item is None or item.state is not EmailState.DELIVERY_UNKNOWN:
            raise RuntimeError("delivery and work item states diverged")
        if found is None and not confirm_absent:
            raise EmailDeliveryConflict(intent)
        action = EmailAction.RECONCILE_SENT if found is not None else EmailAction.RECONCILE_ABSENT
        before = intent.state
        next_state = transition(before, action)
        intent.state = next_state
        intent.last_error_code = None
        intent.version += 1
        item.state = next_state
        item.last_error_code = None
        item.version += 1
        self._db_session.add(
            EmailStateHistory(
                work_item_id=item.id,
                organization_id=item.organization_id,
                from_state=before,
                to_state=next_state,
                action=action,
                actor_id=self._principal.subject_id,
                actor_type="STAFF",
                resource_version=item.version,
            )
        )
        if found is not None:
            self._db_session.add(
                SuccessfulDelivery(
                    delivery_intent_id=intent.id,
                    gmail_message_id=found.gmail_message_id,
                    gmail_thread_id=found.gmail_thread_id,
                    reconciled=True,
                )
            )
            await self._db_session.execute(
                update(JobIntent)
                .where(
                    JobIntent.id == intent.job_id,
                    JobIntent.state == JobState.RECONCILIATION,
                )
                .values(
                    state=JobState.SUCCEEDED,
                    last_error_code=None,
                    error_class=None,
                    version=JobIntent.version + 1,
                    updated_at=func.clock_timestamp(),
                )
            )
        else:
            await self._db_session.execute(
                update(JobIntent)
                .where(
                    JobIntent.id == intent.job_id,
                    JobIntent.state == JobState.RECONCILIATION,
                )
                .values(
                    state=JobState.PENDING,
                    next_attempt_at=None,
                    last_error_code=None,
                    error_class=None,
                    version=JobIntent.version + 1,
                    updated_at=func.clock_timestamp(),
                )
            )
        await self._record(intent, item, found is not None)
        await self._db_session.flush()
        return EmailDeliveryResult(intent.id, item.id, intent.state, intent.version)

    async def _require_access(self, item: EmailWorkItem) -> None:
        if self._principal.role not in {UserRole.ADMIN, UserRole.REVIEWER}:
            raise EmailDeliveryAuthorizationError
        try:
            await self._authorization.require(
                self._principal,
                "knowledge.review",
                ResourceRef(
                    organization_id=item.organization_id,
                    resource_type="knowledge",
                    resource_id=item.knowledge_base_id,
                    state=ResourceState.ACTIVE,
                ),
            )
        except AuthorizationDenied:
            raise EmailDeliveryAuthorizationError from None

    async def _resolve_gateway(self, item: EmailWorkItem) -> GmailReconciliationGateway:
        if self._gateway is not None:
            return self._gateway
        if self._connector_service is None or self._gateway_factory is None:
            raise RuntimeError("encrypted Gmail connector and reconciliation gateway are required")
        connector = await self._db_session.get(Connector, item.connector_id)
        if (
            connector is None
            or connector.organization_id != item.organization_id
            or connector.kind is not ConnectorKind.GMAIL
            or connector.status is not ConnectorStatus.ACTIVE
        ):
            raise RuntimeError("active Gmail connector is unavailable")
        refresh_token = await self._connector_service.load_refresh_token(
            self._db_session, connector
        )
        return (await self._gateway_factory.create(refresh_token=refresh_token)).gateway

    async def _record(self, intent: DeliveryIntent, item: EmailWorkItem, found: bool) -> None:
        details = {
            "delivery_intent_id": str(intent.id),
            "approved_draft_version_id": str(intent.approved_draft_version_id),
            "state": intent.state.value,
            "result": "FOUND" if found else "CONFIRMED_ABSENT",
        }
        await self._audit.record(
            self._db_session,
            self._principal,
            action="email.delivery.reconcile",
            object_type="email_delivery_intent",
            object_id=intent.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            "email.delivery.reconciled",
            "email_delivery_intent",
            intent.id,
            {"organization_id": str(item.organization_id), **details},
        )
