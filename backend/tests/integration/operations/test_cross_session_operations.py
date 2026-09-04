import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.core.database import async_sessionmaker, engine
from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.identity.service import IdentityService, IdentityVersionConflict
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.knowledge.models import DriveSource, KnowledgeBase
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.operations.service import OperationsService
from app.modules.outbox.models import OutboxEvent


async def _seed_identity_users() -> tuple[UUID, UUID, Principal]:
    async with async_sessionmaker() as session:
        organization = Organization(name=f"Task 21 identity {uuid4()}")
        session.add(organization)
        await session.flush()
        admin = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"task21-admin-{uuid4()}",
            email="task21-admin@example.test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        target = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"task21-target-{uuid4()}",
            email="task21-target@example.test",
            role=UserRole.MEMBER,
            status=UserStatus.ACTIVE,
        )
        session.add_all((admin, target))
        await session.flush()
        organization_id = organization.id
        target_id = target.id
        principal = Principal(
            subject_id=admin.id,
            organization_id=organization.id,
            email=admin.email,
            role=admin.role,
            session_id=uuid4(),
            csrf_hash="task21-csrf",
        )
        await session.commit()
    return organization_id, target_id, principal


@pytest.mark.asyncio
async def test_concurrent_user_updates_allow_one_versioned_winner() -> None:
    await engine.dispose()
    organization_id, target_id, principal = await _seed_identity_users()

    async def update_role(role: UserRole) -> StaffUser | IdentityVersionConflict:
        async with async_sessionmaker() as session:
            try:
                updated = await IdentityService(session).update_staff(
                    principal,
                    target_id,
                    expected_version=1,
                    role=role,
                    status=None,
                )
                await session.commit()
                return updated
            except IdentityVersionConflict as error:
                await session.rollback()
                return error

    try:
        results = await asyncio.gather(
            update_role(UserRole.REVIEWER),
            update_role(UserRole.ADMIN),
        )
        async with async_sessionmaker() as verify:
            stored = await verify.get(StaffUser, target_id)
            audit_count = len(
                list(
                    (
                        await verify.scalars(
                            select(AuditEvent.id).where(AuditEvent.object_id == target_id)
                        )
                    ).all()
                )
            )
            outbox_count = len(
                list(
                    (
                        await verify.scalars(
                            select(OutboxEvent.event_id).where(
                                OutboxEvent.aggregate_id == target_id
                            )
                        )
                    ).all()
                )
            )
        assert sum(isinstance(result, StaffUser) for result in results) == 1
        assert sum(isinstance(result, IdentityVersionConflict) for result in results) == 1
        assert stored is not None and stored.version == 2
        assert stored.role in {UserRole.REVIEWER, UserRole.ADMIN}
        assert audit_count == 1
        assert outbox_count == 1
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(
                delete(AuditEvent).where(AuditEvent.organization_id == organization_id)
            )
            await cleanup.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id == target_id))
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()


async def _seed_failed_drive_job() -> tuple[UUID, UUID, Principal, dict[str, object], str]:
    async with async_sessionmaker() as session:
        organization = Organization(name=f"Task 21 retry {uuid4()}")
        session.add(organization)
        await session.flush()
        admin = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"task21-retry-admin-{uuid4()}",
            email="task21-retry-admin@example.test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        knowledge_base = KnowledgeBase(organization_id=organization.id)
        session.add_all((admin, knowledge_base))
        await session.flush()
        source = DriveSource(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            root_folder_id="task21-root",
            include_descendants=True,
            allowed_descendant_ids=["task21-child"],
            connection_identity="task21-drive-reader@example.test",
        )
        session.add(source)
        await session.flush()
        session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=admin.id,
                resource_type="knowledge",
                resource_id=KnowledgeSourceService.configuration_resource_id(organization.id),
                actions=["knowledge.write"],
            )
        )
        payload: dict[str, object] = {
            "source_id": str(source.id),
            "page_token": "durable-cursor",
        }
        idempotency_key = f"task21-retry-{uuid4()}"
        job = JobIntent(
            kind="knowledge.drive_source.sync",
            idempotency_key=idempotency_key,
            payload=payload,
            state=JobState.FAILED,
            last_error_code="DRIVE_RATE_LIMITED",
            error_class=ErrorClass.RETRYABLE,
        )
        session.add(job)
        await session.flush()
        principal = Principal(
            subject_id=admin.id,
            organization_id=organization.id,
            email=admin.email,
            role=admin.role,
            session_id=uuid4(),
            csrf_hash="task21-csrf",
        )
        organization_id = organization.id
        job_id = job.id
        await session.commit()
    return organization_id, job_id, principal, payload, idempotency_key


@pytest.mark.asyncio
async def test_drive_retry_is_durable_and_recoverable_across_sessions() -> None:
    await engine.dispose()
    organization_id, job_id, principal, payload, idempotency_key = await _seed_failed_drive_job()
    try:
        async with async_sessionmaker() as retry_session:
            result = await OperationsService(retry_session).retry_job(principal, job_id)
            assert result.job_id == job_id
            await retry_session.commit()

        async with async_sessionmaker() as verify:
            stored = await verify.get(JobIntent, job_id)
            audit = await verify.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "knowledge.drive_source.sync.retry",
                    AuditEvent.object_id == job_id,
                )
            )
            outbox = await verify.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "knowledge.drive_source.sync.requested",
                    OutboxEvent.aggregate_id == job_id,
                )
            )
        assert stored is not None and stored.state is JobState.PENDING
        assert stored.payload == payload
        assert stored.idempotency_key == idempotency_key
        assert audit is not None
        assert outbox is not None
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(
                delete(AuditEvent).where(AuditEvent.organization_id == organization_id)
            )
            await cleanup.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))
            await cleanup.execute(delete(JobIntent).where(JobIntent.id == job_id))
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()
