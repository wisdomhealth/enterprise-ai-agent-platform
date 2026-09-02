import re
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.models import ChatSession, ConversationState
from app.modules.connectors.models import Connector
from app.modules.connectors.schemas import GOOGLE_CONNECTOR_SCOPES
from app.modules.connectors.service import ConnectorService
from app.modules.email.delivery import (
    EmailDeliveryAuthorizationError,
    EmailDeliveryConflict,
    EmailDeliveryService,
    ReconciliationRequired,
)
from app.modules.email.models import DeliveryIntent, EmailEvaluationRun, EmailState, EmailWorkItem
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.knowledge.models import (
    Document,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
)
from app.modules.knowledge.operations import DriveSyncOperations
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.operations.schemas import (
    ConnectorReauthorizationRead,
    ConnectorStatusRead,
    EmailSummaryRead,
    FailedJobRead,
    JobAction,
    JobRetryRead,
    JobSummaryRead,
    KnowledgeSourceStatusRead,
    OperationsSummaryRead,
    QualityStatusRead,
    SupportSummaryRead,
)
from app.modules.outbox.service import OutboxService
from app.modules.rag.evaluation_models import RAGEvaluationRun
from app.modules.support.models import Handoff


class OperationsNotFound(LookupError):
    pass


class DeliveryReconciliationOnly(RuntimeError):
    def __init__(self, delivery_intent_id: UUID) -> None:
        self.delivery_intent_id = delivery_intent_id


class OperationsService:
    _SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,99}\Z")

    def __init__(
        self,
        db_session: AsyncSession,
        *,
        connector_service: ConnectorService | None = None,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._connector_service = connector_service
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()

    async def summary(self, principal: Principal) -> OperationsSummaryRead:
        self.require_admin(principal)
        connector_ids = await self._granted_resource_ids(principal, "connector")
        connectors = list(
            (
                await self._db_session.scalars(
                    select(Connector)
                    .where(
                        Connector.organization_id == principal.organization_id,
                        Connector.id.in_(connector_ids),
                    )
                    .order_by(Connector.kind, Connector.id)
                )
            ).all()
        )
        can_manage_sources = await self._has_action_grant(
            principal,
            resource_type="knowledge",
            resource_id=KnowledgeSourceService.configuration_resource_id(principal.organization_id),
            action="knowledge.write",
        )
        sources = (
            list(
                (
                    await self._db_session.scalars(
                        select(DriveSource)
                        .where(DriveSource.organization_id == principal.organization_id)
                        .order_by(DriveSource.id)
                    )
                ).all()
            )
            if can_manage_sources
            else []
        )
        source_statuses = [await self._source_status(source) for source in sources]
        source_ids = {str(source.id) for source in sources}
        reviewable_knowledge_base_ids = await self._granted_resource_ids(
            principal, "knowledge", action="knowledge.review"
        )
        delivery_job_ids = set(
            (
                await self._db_session.scalars(
                    select(DeliveryIntent.job_id)
                    .join(EmailWorkItem, EmailWorkItem.id == DeliveryIntent.work_item_id)
                    .where(
                        DeliveryIntent.organization_id == principal.organization_id,
                        EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                    )
                )
            ).all()
        )
        jobs = list((await self._db_session.scalars(select(JobIntent))).all())
        visible_jobs = [
            job
            for job in jobs
            if (
                job.kind == "knowledge.drive_source.sync"
                and str(job.payload.get("source_id")) in source_ids
            )
            or job.id in delivery_job_ids
        ]
        email_retry_wait = int(
            await self._db_session.scalar(
                select(func.count(EmailWorkItem.id)).where(
                    EmailWorkItem.organization_id == principal.organization_id,
                    EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                    EmailWorkItem.state.in_(
                        [EmailState.DRAFT_RETRY_WAIT, EmailState.SEND_RETRY_WAIT]
                    ),
                )
            )
            or 0
        )
        delivery_unknown = int(
            await self._db_session.scalar(
                select(func.count(DeliveryIntent.id))
                .join(EmailWorkItem, EmailWorkItem.id == DeliveryIntent.work_item_id)
                .where(
                    DeliveryIntent.organization_id == principal.organization_id,
                    EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                    DeliveryIntent.state == EmailState.DELIVERY_UNKNOWN,
                )
            )
            or 0
        )
        support_backlog = int(
            await self._db_session.scalar(
                select(func.count(Handoff.id))
                .join(ChatSession, ChatSession.id == Handoff.session_id)
                .where(
                    Handoff.organization_id == principal.organization_id,
                    ChatSession.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                    Handoff.state.in_(
                        [ConversationState.HANDOFF_REQUESTED, ConversationState.QUEUED]
                    ),
                )
            )
            or 0
        )
        generated_at = await self._db_session.scalar(select(func.clock_timestamp()))
        if not isinstance(generated_at, datetime):
            raise RuntimeError("database clock is unavailable")
        return OperationsSummaryRead(
            generated_at=generated_at,
            connectors=[
                ConnectorStatusRead(
                    id=connector.id,
                    kind=connector.kind,
                    status=connector.status,
                    updated_at=connector.updated_at,
                    requested_scopes=list(GOOGLE_CONNECTOR_SCOPES[connector.kind]),
                )
                for connector in connectors
            ],
            knowledge_sources=source_statuses,
            jobs=JobSummaryRead(
                queue_depth=sum(
                    job.state in {JobState.PENDING, JobState.RUNNING, JobState.RECONCILIATION}
                    for job in visible_jobs
                ),
                failed=sum(job.state is JobState.FAILED for job in visible_jobs),
            ),
            support=SupportSummaryRead(backlog=support_backlog),
            email=EmailSummaryRead(
                retry_wait=email_retry_wait,
                delivery_unknown=delivery_unknown,
            ),
            rag_quality=await self._latest_rag_quality(
                principal.organization_id,
                await self._granted_resource_ids(principal, "knowledge", action="knowledge.read"),
            ),
            email_quality=await self._latest_email_quality(),
        )

    async def failed_jobs(self, principal: Principal) -> list[FailedJobRead]:
        self.require_admin(principal)
        can_manage_sources = await self._has_action_grant(
            principal,
            resource_type="knowledge",
            resource_id=KnowledgeSourceService.configuration_resource_id(principal.organization_id),
            action="knowledge.write",
        )
        source_ids = set(
            (
                await self._db_session.scalars(
                    select(DriveSource.id).where(
                        DriveSource.organization_id == principal.organization_id
                    )
                )
            ).all()
            if can_manage_sources
            else []
        )
        reviewable_knowledge_base_ids = await self._granted_resource_ids(
            principal, "knowledge", action="knowledge.review"
        )
        delivery_intents = list(
            (
                await self._db_session.scalars(
                    select(DeliveryIntent)
                    .join(EmailWorkItem, EmailWorkItem.id == DeliveryIntent.work_item_id)
                    .where(
                        DeliveryIntent.organization_id == principal.organization_id,
                        EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                    )
                )
            ).all()
        )
        delivery_by_job = {intent.job_id: intent for intent in delivery_intents}
        jobs = list(
            (
                await self._db_session.scalars(
                    select(JobIntent)
                    .where(
                        JobIntent.state.in_(
                            [JobState.PENDING, JobState.FAILED, JobState.RECONCILIATION]
                        )
                    )
                    .order_by(JobIntent.updated_at.desc(), JobIntent.id)
                )
            ).all()
        )
        results: list[FailedJobRead] = []
        for job in jobs:
            intent = delivery_by_job.get(job.id)
            source_id = self._payload_uuid(job, "source_id")
            if (
                source_id in source_ids
                and job.kind == "knowledge.drive_source.sync"
                and job.state in {JobState.FAILED, JobState.RECONCILIATION}
            ):
                action = (
                    JobAction.RETRY_DRIVE_SYNC
                    if job.state is JobState.FAILED and job.error_class is ErrorClass.RETRYABLE
                    else JobAction.NONE
                )
                action_resource_id = source_id
            elif intent is not None:
                action_resource_id = intent.work_item_id
                if intent.state is EmailState.DELIVERY_UNKNOWN:
                    action = JobAction.RECONCILE_GMAIL
                elif intent.state is EmailState.SEND_RETRY_WAIT:
                    action = JobAction.RETRY_EMAIL_DELIVERY
                else:
                    continue
            else:
                continue
            results.append(
                FailedJobRead(
                    job_id=job.id,
                    kind=job.kind,
                    state=job.state.value,
                    attempts=job.attempts,
                    error_code=self._safe_error_code(job.last_error_code),
                    updated_at=job.updated_at,
                    action=action,
                    action_resource_id=action_resource_id,
                )
            )
        return results

    async def retry_job(self, principal: Principal, job_id: UUID) -> JobRetryRead:
        self.require_admin(principal)
        job = await self._db_session.get(JobIntent, job_id)
        if job is None:
            raise OperationsNotFound(job_id)
        if job.kind == "knowledge.drive_source.sync":
            try:
                retried = await DriveSyncOperations(self._db_session).retry_failed_job(
                    principal=principal, job_id=job_id
                )
            except HTTPException as error:
                if error.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}:
                    raise OperationsNotFound(job_id) from None
                raise
            return JobRetryRead(
                job_id=retried.id,
                state=retried.state.value,
                version=retried.version,
            )
        if job.kind == "email.delivery":
            intent = await self._db_session.scalar(
                select(DeliveryIntent).where(
                    DeliveryIntent.job_id == job.id,
                    DeliveryIntent.organization_id == principal.organization_id,
                )
            )
            if intent is None:
                raise OperationsNotFound(job_id)
            if intent.state is EmailState.DELIVERY_UNKNOWN:
                raise DeliveryReconciliationOnly(intent.id)
            try:
                result = await EmailDeliveryService(
                    self._db_session, worker_id="admin-manual-retry"
                ).request_retry(intent.id, principal, expected_version=intent.version)
            except (EmailDeliveryAuthorizationError, LookupError):
                raise OperationsNotFound(job_id) from None
            except EmailDeliveryConflict as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "JOB_VERSION_CONFLICT",
                        "state": error.state.value,
                        "version": error.version,
                    },
                ) from None
            except ReconciliationRequired as error:
                raise DeliveryReconciliationOnly(intent.id) from error
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "JOB_NOT_RETRYABLE"},
                ) from None
            return JobRetryRead(job_id=job.id, state=result.state.value, version=result.version)
        raise OperationsNotFound(job_id)

    async def begin_connector_reauthorization(
        self, principal: Principal, connector_id: UUID
    ) -> ConnectorReauthorizationRead:
        self.require_admin(principal)
        connector = await self._db_session.scalar(
            select(Connector).where(
                Connector.id == connector_id,
                Connector.organization_id == principal.organization_id,
            )
        )
        if connector is None:
            raise OperationsNotFound(connector_id)
        if self._connector_service is None:
            raise RuntimeError("connector encryption is not configured")
        try:
            await self._connector_service.require_authorization_start(
                self._db_session, principal=principal, kind=connector.kind
            )
        except HTTPException as error:
            if error.status_code == status.HTTP_403_FORBIDDEN:
                raise OperationsNotFound(connector_id) from None
            raise
        details = {"kind": connector.kind.value, "status": connector.status.value}
        await self._audit.record(
            self._db_session,
            principal,
            action="connector.reauthorization.start",
            object_type="connector",
            object_id=connector.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            "connector.reauthorization.started",
            "connector",
            connector.id,
            {"organization_id": str(principal.organization_id), **details},
        )
        return ConnectorReauthorizationRead(
            connector_id=connector.id,
            authorization_url=f"/api/v1/admin/connectors/{connector.kind.value}/authorize",
            requested_scopes=list(GOOGLE_CONNECTOR_SCOPES[connector.kind]),
        )

    @staticmethod
    def require_admin(principal: Principal) -> None:
        if principal.role is not UserRole.ADMIN:
            raise OperationsNotFound(principal.subject_id)

    async def _source_status(self, source: DriveSource) -> KnowledgeSourceStatusRead:
        jobs = list(
            (
                await self._db_session.scalars(
                    select(JobIntent).where(JobIntent.kind == "knowledge.drive_source.sync")
                )
            ).all()
        )
        source_jobs = [job for job in jobs if job.payload.get("source_id") == str(source.id)]
        document_ids = list(
            (
                await self._db_session.scalars(
                    select(Document.id).where(Document.source_id == source.id)
                )
            ).all()
        )
        isolated = 0
        if document_ids:
            isolated = int(
                await self._db_session.scalar(
                    select(func.count(DocumentVersion.id)).where(
                        DocumentVersion.document_id.in_(document_ids),
                        DocumentVersion.state.in_(
                            [DocumentVersionState.FAILED, DocumentVersionState.REVOKED]
                        ),
                    )
                )
                or 0
            )
        successful = [job.updated_at for job in source_jobs if job.state is JobState.SUCCEEDED]
        errors = [
            self._safe_error_code(job.last_error_code) for job in source_jobs if job.last_error_code
        ]
        return KnowledgeSourceStatusRead(
            source_id=source.id,
            status=source.status.value,
            root_folder_id=source.root_folder_id,
            include_descendants=source.include_descendants,
            descendant_count=len(source.allowed_descendant_ids),
            cursor=source.sync_cursor,
            last_success_at=max(successful, default=None),
            backlog=sum(job.state in {JobState.PENDING, JobState.RUNNING} for job in source_jobs),
            isolated_files=isolated,
            retry_count=sum(job.attempts for job in source_jobs),
            recent_error_codes=[error for error in errors[-5:] if error is not None],
        )

    @classmethod
    def _safe_error_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if cls._SAFE_ERROR_CODE.fullmatch(value):
            return value
        return "UNSAFE_ERROR_REDACTED"

    async def _latest_rag_quality(
        self, organization_id: UUID, knowledge_base_ids: set[UUID]
    ) -> QualityStatusRead | None:
        run = await self._db_session.scalar(
            select(RAGEvaluationRun)
            .where(
                RAGEvaluationRun.organization_id == organization_id,
                RAGEvaluationRun.knowledge_base_id.in_(knowledge_base_ids),
            )
            .order_by(RAGEvaluationRun.completed_at.desc())
            .limit(1)
        )
        if run is None:
            return None
        metrics = run.metrics
        return QualityStatusRead(
            completed_at=run.completed_at,
            status=run.status,
            quality_score=self._safe_metric(metrics, "answer_groundedness"),
            latency_ms=self._safe_metric(metrics, "end_to_end_latency_ms"),
            estimated_cost=self._safe_metric(metrics, "estimated_cost"),
        )

    @staticmethod
    def _safe_metric(metrics: dict[str, object], key: str) -> float:
        value = metrics.get(key, 0.0)
        if isinstance(value, int | float):
            return float(value)
        return 0.0

    async def _latest_email_quality(self) -> QualityStatusRead | None:
        run = await self._db_session.scalar(
            select(EmailEvaluationRun).order_by(EmailEvaluationRun.created_at.desc()).limit(1)
        )
        if run is None:
            return None
        return QualityStatusRead(
            completed_at=run.created_at,
            status="COMPLETED",
            quality_score=run.macro_f1,
            latency_ms=float(run.latency_ms),
            estimated_cost=run.estimated_cost,
        )

    async def _granted_resource_ids(
        self,
        principal: Principal,
        resource_type: str,
        *,
        action: str | None = None,
    ) -> set[UUID]:
        statement = select(ResourceGrant.resource_id).where(
            ResourceGrant.organization_id == principal.organization_id,
            ResourceGrant.subject_id == principal.subject_id,
            ResourceGrant.resource_type == resource_type,
        )
        if action is not None:
            statement = statement.where(ResourceGrant.actions.contains([action]))
        return set((await self._db_session.scalars(statement)).all())

    async def _has_action_grant(
        self,
        principal: Principal,
        *,
        resource_type: str,
        resource_id: UUID,
        action: str,
    ) -> bool:
        return bool(
            await self._db_session.scalar(
                select(ResourceGrant.id).where(
                    ResourceGrant.organization_id == principal.organization_id,
                    ResourceGrant.subject_id == principal.subject_id,
                    ResourceGrant.resource_type == resource_type,
                    ResourceGrant.resource_id == resource_id,
                    ResourceGrant.actions.contains([action]),
                )
            )
        )

    @staticmethod
    def _payload_uuid(job: JobIntent, key: str) -> UUID | None:
        try:
            return UUID(str(job.payload.get(key)))
        except (TypeError, ValueError):
            return None
