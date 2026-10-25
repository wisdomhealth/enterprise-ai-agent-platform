import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import String, and_, case, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import CompoundSelect

from app.core.config import Settings
from app.modules.audit.models import AuditEvent
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
    FailedJobPageRead,
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
        source_statuses = await self._source_statuses(sources)
        source_ids = {str(source.id) for source in sources}
        reviewable_knowledge_base_ids = await self._granted_resource_ids(
            principal, "knowledge", action="knowledge.review"
        )
        queue_depth, failed_count = await self._job_counts(
            principal.organization_id,
            source_ids=source_ids,
            reviewable_knowledge_base_ids=reviewable_knowledge_base_ids,
        )
        if reviewable_knowledge_base_ids:
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
        else:
            email_retry_wait = 0
            delivery_unknown = 0
            support_backlog = 0
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
                queue_depth=queue_depth,
                failed=failed_count,
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
            email_quality=await self._latest_email_quality(
                has_authorized_email_resource=bool(reviewable_knowledge_base_ids)
            ),
        )

    async def failed_jobs(
        self,
        principal: Principal,
        *,
        page: int = 1,
        page_size: int = 20,
        kind: str | None = None,
        state: str | None = None,
        updated_from: datetime | None = None,
        updated_to: datetime | None = None,
    ) -> FailedJobPageRead:
        self.require_admin(principal)
        can_manage_sources = await self._has_action_grant(
            principal,
            resource_type="knowledge",
            resource_id=KnowledgeSourceService.configuration_resource_id(principal.organization_id),
            action="knowledge.write",
        )
        source_ids = {
            str(source_id)
            for source_id in (
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
        }
        reviewable_knowledge_base_ids = await self._granted_resource_ids(
            principal, "knowledge", action="knowledge.review"
        )
        visible_jobs = self._failed_job_rows(
            principal.organization_id,
            source_ids=source_ids,
            reviewable_knowledge_base_ids=reviewable_knowledge_base_ids,
        ).subquery()
        filters = []
        if kind is not None:
            filters.append(visible_jobs.c.kind == kind)
        if state is not None:
            filters.append(visible_jobs.c.state == state)
        if updated_from is not None:
            filters.append(visible_jobs.c.updated_at >= updated_from)
        if updated_to is not None:
            filters.append(visible_jobs.c.updated_at <= updated_to)
        total = int(
            await self._db_session.scalar(
                select(func.count()).select_from(visible_jobs).where(*filters)
            )
            or 0
        )
        rows = (
            await self._db_session.execute(
                select(visible_jobs)
                .where(*filters)
                .order_by(visible_jobs.c.updated_at.desc(), visible_jobs.c.job_id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).mappings()
        items = [
            FailedJobRead(
                job_id=row["job_id"],
                kind=row["kind"],
                state=row["state"],
                attempts=row["attempts"],
                last_error_code=self._safe_error_code(row["last_error_code"]),
                error_category=row["error_category"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                action=row["action"],
                action_resource_id=row["action_resource_id"],
            )
            for row in rows
        ]
        return FailedJobPageRead(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            count=len(items),
            total_pages=(total + page_size - 1) // page_size,
        )

    def _failed_job_rows(
        self,
        organization_id: UUID,
        *,
        source_ids: set[str],
        reviewable_knowledge_base_ids: set[UUID],
    ) -> CompoundSelect[Any]:
        source_id = JobIntent.payload["source_id"].astext
        work_item_id = JobIntent.payload["work_item_id"].astext
        columns = (
            JobIntent.id.label("job_id"),
            JobIntent.kind,
            JobIntent.state,
            JobIntent.attempts,
            JobIntent.last_error_code,
            cast(JobIntent.error_class, String).label("error_category"),
            JobIntent.created_at,
            JobIntent.updated_at,
        )
        drive_jobs = select(
            *columns,
            case(
                (
                    and_(
                        JobIntent.state == JobState.FAILED,
                        JobIntent.error_class == ErrorClass.RETRYABLE,
                    ),
                    literal(JobAction.RETRY_DRIVE_SYNC.value),
                ),
                else_=literal(JobAction.NONE.value),
            ).label("action"),
            source_id.label("action_resource_id"),
        ).where(
            JobIntent.kind == "knowledge.drive_source.sync",
            JobIntent.state.in_([JobState.FAILED, JobState.RECONCILIATION]),
            source_id.in_(source_ids),
        )
        classify_jobs = (
            select(
                *columns,
                literal(JobAction.RETRY_EMAIL_CLASSIFICATION.value).label("action"),
                cast(EmailWorkItem.id, String).label("action_resource_id"),
            )
            .join(EmailWorkItem, cast(EmailWorkItem.id, String) == work_item_id)
            .where(
                JobIntent.kind == "email.classify",
                JobIntent.state == JobState.FAILED,
                EmailWorkItem.organization_id == organization_id,
                EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
            )
        )
        delivery_jobs = (
            select(
                *columns,
                case(
                    (
                        DeliveryIntent.state == EmailState.DELIVERY_UNKNOWN,
                        literal(JobAction.RECONCILE_GMAIL.value),
                    ),
                    else_=literal(JobAction.RETRY_EMAIL_DELIVERY.value),
                ).label("action"),
                cast(DeliveryIntent.work_item_id, String).label("action_resource_id"),
            )
            .join(DeliveryIntent, DeliveryIntent.job_id == JobIntent.id)
            .join(EmailWorkItem, EmailWorkItem.id == DeliveryIntent.work_item_id)
            .where(
                JobIntent.state.in_(
                    [JobState.PENDING, JobState.FAILED, JobState.RECONCILIATION]
                ),
                DeliveryIntent.organization_id == organization_id,
                EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                DeliveryIntent.state.in_(
                    [EmailState.DELIVERY_UNKNOWN, EmailState.SEND_RETRY_WAIT]
                ),
            )
        )
        return union_all(drive_jobs, classify_jobs, delivery_jobs)

    async def _job_counts(
        self,
        organization_id: UUID,
        *,
        source_ids: set[str],
        reviewable_knowledge_base_ids: set[UUID],
    ) -> tuple[int, int]:
        conditions = []
        if source_ids:
            conditions.append(
                and_(
                    JobIntent.kind == "knowledge.drive_source.sync",
                    JobIntent.payload["source_id"].astext.in_(source_ids),
                )
            )
        if reviewable_knowledge_base_ids:
            delivery_visible = exists(
                select(DeliveryIntent.id)
                .join(EmailWorkItem, EmailWorkItem.id == DeliveryIntent.work_item_id)
                .where(
                    DeliveryIntent.job_id == JobIntent.id,
                    DeliveryIntent.organization_id == organization_id,
                    EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                )
            )
            classify_visible = exists(
                select(EmailWorkItem.id).where(
                    cast(EmailWorkItem.id, String)
                    == JobIntent.payload["work_item_id"].astext,
                    EmailWorkItem.organization_id == organization_id,
                    EmailWorkItem.knowledge_base_id.in_(reviewable_knowledge_base_ids),
                )
            )
            conditions.extend(
                (
                    delivery_visible,
                    and_(JobIntent.kind == "email.classify", classify_visible),
                )
            )
        if not conditions:
            return 0, 0
        row = (
            await self._db_session.execute(
                select(
                    func.count()
                    .filter(
                        JobIntent.state.in_(
                            [JobState.PENDING, JobState.RUNNING, JobState.RECONCILIATION]
                        )
                    )
                    .label("queue_depth"),
                    func.count()
                    .filter(JobIntent.state == JobState.FAILED)
                    .label("failed"),
                ).where(or_(*conditions))
            )
        ).one()
        return int(row.queue_depth), int(row.failed)

    async def retry_job(
        self,
        principal: Principal,
        job_id: UUID,
        *,
        reason: str | None = None,
        expected_version: int | None = None,
    ) -> JobRetryRead:
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
        if job.kind == "email.classify":
            return await self._retry_email_classification(principal, job_id)
        if job.kind == "email.draft":
            return await self._retry_email_draft(
                principal,
                job_id,
                reason=reason,
                expected_version=expected_version,
            )
        raise OperationsNotFound(job_id)

    async def _retry_email_draft(
        self,
        principal: Principal,
        job_id: UUID,
        *,
        reason: str | None,
        expected_version: int | None,
    ) -> JobRetryRead:
        if reason is None or not reason.strip() or expected_version is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "DRAFT_RETRY_REASON_AND_VERSION_REQUIRED"},
            )
        job = await self._db_session.scalar(
            select(JobIntent).where(JobIntent.id == job_id).with_for_update()
        )
        if job is None or job.kind != "email.draft":
            raise OperationsNotFound(job_id)
        if job.version != expected_version or job.state is not JobState.FAILED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "JOB_NOT_RETRYABLE"},
            )
        item = await self._db_session.scalar(
            select(EmailWorkItem)
            .where(
                EmailWorkItem.id == self._payload_uuid(job, "work_item_id"),
                EmailWorkItem.organization_id == principal.organization_id,
            )
            .with_for_update()
        )
        if item is None or item.state is not EmailState.DRAFT_RETRY_WAIT:
            raise OperationsNotFound(job_id)
        reviewable_ids = await self._granted_resource_ids(
            principal, "knowledge", action="knowledge.review"
        )
        if item.knowledge_base_id not in reviewable_ids:
            raise OperationsNotFound(job_id)
        job.attempt_cycle = (job.attempt_cycle or 1) + 1
        job.cycle_start_attempts = job.attempts
        job.cycle_attempt_limit = Settings().email_draft_max_attempts
        job.state = JobState.PENDING
        job.next_attempt_at = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error_code = None
        job.error_class = None
        job.version += 1
        item.active_draft_job_id = job.id
        await self._outbox.add(
            self._db_session,
            "email.draft.requested",
            "job",
            job.id,
            {
                "job_id": str(job.id),
                "work_item_id": str(item.id),
                "organization_id": str(item.organization_id),
                "attempt_cycle": job.attempt_cycle,
            },
        )
        details = {
            "work_item_id": str(item.id),
            "reason": reason.strip(),
            "attempt_cycle": job.attempt_cycle,
            "cycle_start_attempts": job.cycle_start_attempts,
            "cycle_attempt_limit": job.cycle_attempt_limit,
        }
        await self._audit.record(
            self._db_session,
            principal,
            action="email.draft.retry_cycle.start",
            object_type="job",
            object_id=job.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._db_session.flush()
        return JobRetryRead(job_id=job.id, state=job.state.value, version=job.version)

    async def _retry_email_classification(
        self, principal: Principal, job_id: UUID
    ) -> JobRetryRead:
        job = await self._db_session.scalar(
            select(JobIntent).where(JobIntent.id == job_id).with_for_update()
        )
        if job is None or job.kind != "email.classify":
            raise OperationsNotFound(job_id)
        work_item_id = self._payload_uuid(job, "work_item_id")
        reviewable_ids = await self._granted_resource_ids(
            principal, "knowledge", action="knowledge.review"
        )
        item = await self._db_session.scalar(
            select(EmailWorkItem)
            .where(
                EmailWorkItem.id == work_item_id,
                EmailWorkItem.organization_id == principal.organization_id,
                EmailWorkItem.knowledge_base_id.in_(reviewable_ids),
            )
            .with_for_update()
        )
        if item is None:
            raise OperationsNotFound(job_id)
        if (
            job.state is not JobState.FAILED
            or item.category is not None
            or item.state not in {EmailState.INGESTED, EmailState.DRAFT_RETRY_WAIT}
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "JOB_NOT_RETRYABLE"},
            )
        previous_attempts = job.attempts
        previous_error_code = self._safe_error_code(job.last_error_code)
        job.state = JobState.PENDING
        job.attempts = 0
        job.next_attempt_at = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error_code = None
        job.error_class = None
        job.version += 1
        item.last_error_code = None
        details = {
            "work_item_id": str(item.id),
            "previous_attempts": previous_attempts,
            "previous_error_code": previous_error_code,
        }
        await self._audit.record(
            self._db_session,
            principal,
            action="email.classification.retry",
            object_type="job",
            object_id=job.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            "email.classify.requested",
            "job",
            job.id,
            {
                "work_item_id": str(item.id),
                "organization_id": str(item.organization_id),
            },
        )
        await self._db_session.flush()
        return JobRetryRead(job_id=job.id, state=job.state.value, version=job.version)

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

    async def _source_statuses(
        self, sources: list[DriveSource]
    ) -> list[KnowledgeSourceStatusRead]:
        if not sources:
            return []
        source_ids = {str(source.id) for source in sources}
        source_key = JobIntent.payload["source_id"].astext
        job_rows = (
            await self._db_session.execute(
                select(
                    source_key.label("source_id"),
                    func.max(JobIntent.updated_at)
                    .filter(JobIntent.state == JobState.SUCCEEDED)
                    .label("last_success_at"),
                    func.count()
                    .filter(JobIntent.state.in_([JobState.PENDING, JobState.RUNNING]))
                    .label("backlog"),
                    func.coalesce(func.sum(JobIntent.attempts), 0).label("retry_count"),
                )
                .where(
                    JobIntent.kind == "knowledge.drive_source.sync",
                    source_key.in_(source_ids),
                )
                .group_by(source_key)
            )
        ).mappings()
        jobs_by_source = {row["source_id"]: row for row in job_rows}

        ranked_errors = (
            select(
                source_key.label("source_id"),
                JobIntent.last_error_code.label("error_code"),
                func.row_number()
                .over(
                    partition_by=source_key,
                    order_by=(JobIntent.updated_at.desc(), JobIntent.id.desc()),
                )
                .label("position"),
            )
            .where(
                JobIntent.kind == "knowledge.drive_source.sync",
                source_key.in_(source_ids),
                JobIntent.last_error_code.is_not(None),
            )
            .subquery()
        )
        error_rows = (
            await self._db_session.execute(
                select(ranked_errors.c.source_id, ranked_errors.c.error_code)
                .where(ranked_errors.c.position <= 5)
                .order_by(ranked_errors.c.source_id, ranked_errors.c.position)
            )
        ).all()
        errors_by_source: dict[str, list[str]] = defaultdict(list)
        for source_id, error_code in error_rows:
            safe_code = self._safe_error_code(error_code)
            if safe_code is not None:
                errors_by_source[source_id].append(safe_code)

        isolated_rows = (
            await self._db_session.execute(
                select(Document.source_id, func.count(DocumentVersion.id))
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .where(
                    cast(Document.source_id, String).in_(source_ids),
                    DocumentVersion.state.in_(
                        [DocumentVersionState.FAILED, DocumentVersionState.REVOKED]
                    ),
                )
                .group_by(Document.source_id)
            )
        ).all()
        isolated_by_source = {str(source_id): int(count) for source_id, count in isolated_rows}

        disabled_sources = [source for source in sources if source.status.value == "DISABLED"]
        disabled_reasons: dict[UUID, str] = {}
        if disabled_sources:
            events = (
                await self._db_session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.organization_id == disabled_sources[0].organization_id,
                        AuditEvent.object_id.in_([source.id for source in disabled_sources]),
                        AuditEvent.action.in_(
                            (
                                "knowledge.drive_source.root_unavailable",
                                "knowledge.drive_source.configure",
                            )
                        ),
                    )
                    .distinct(AuditEvent.object_id)
                    .order_by(
                        AuditEvent.object_id,
                        AuditEvent.occurred_at.desc(),
                        AuditEvent.id.desc(),
                    )
                )
            ).all()
            for event in events:
                reason = event.details.get("reason")
                if event.action == "knowledge.drive_source.root_unavailable" and isinstance(
                    reason, str
                ):
                    disabled_reasons[event.object_id] = f"ROOT_UNAVAILABLE_{reason}"

        results = []
        for source in sources:
            source_id = str(source.id)
            statistics = jobs_by_source.get(source_id)
            results.append(
                KnowledgeSourceStatusRead(
                    source_id=source.id,
                    status=source.status.value,
                    root_folder_id=source.root_folder_id,
                    include_descendants=source.include_descendants,
                    descendant_count=len(source.allowed_descendant_ids),
                    cursor=source.sync_cursor,
                    last_success_at=(
                        statistics["last_success_at"] if statistics is not None else None
                    ),
                    backlog=int(statistics["backlog"]) if statistics is not None else 0,
                    isolated_files=isolated_by_source.get(source_id, 0),
                    retry_count=(
                        int(statistics["retry_count"]) if statistics is not None else 0
                    ),
                    recent_error_codes=errors_by_source[source_id],
                    disabled_reason=disabled_reasons.get(source.id),
                )
            )
        return results

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

    async def _latest_email_quality(
        self, *, has_authorized_email_resource: bool
    ) -> QualityStatusRead | None:
        # EmailEvaluationRun is a product-level baseline without resource ownership.
        # Expose it only when this principal can review at least one email knowledge base.
        if not has_authorized_email_resource:
            return None
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
