from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent
from app.modules.jobs.service import JobService
from app.modules.knowledge.models import DriveSource, KnowledgeBase
from app.modules.knowledge.operations import DriveSyncOperations
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.knowledge.sync import drive_sync_job_key
from app.modules.knowledge.tasks import (
    _dispatch_drive_sync_outbox_event,
    _dispatch_pending_drive_sync_outbox_events,
    dispatch_drive_sync_outbox_event,
    dispatch_pending_drive_sync_outbox_events,
    drive_source_sync,
)
from app.modules.outbox.models import OutboxEvent


@pytest.mark.asyncio
async def test_duplicate_manual_retries_preserve_one_durable_intent(db_session) -> None:  # type: ignore[no-untyped-def]
    source_id = uuid4()
    key = drive_sync_job_key(source_id, "cursor-1")
    service = JobService()

    first = await service.enqueue(
        db_session,
        "knowledge.drive_source.sync",
        key,
        {"source_id": str(source_id), "page_token": "cursor-1"},
    )
    second = await service.enqueue(
        db_session,
        "knowledge.drive_source.sync",
        key,
        {"source_id": str(source_id), "page_token": "cursor-1"},
    )
    await db_session.commit()

    assert first.id == second.id
    assert await db_session.scalar(select(func.count(JobIntent.id))) == 1


def test_manual_and_scheduled_sync_share_one_durable_intent_key() -> None:
    assert drive_sync_job_key("source", "cursor") == drive_sync_job_key("source", "cursor")


def test_periodic_celery_sync_task_is_registered_under_the_scheduled_name() -> None:
    assert drive_source_sync.name == "app.modules.knowledge.tasks.drive_source_sync"


def test_outbox_dispatcher_is_registered_for_drive_sync_delivery() -> None:
    assert (
        dispatch_drive_sync_outbox_event.name
        == "app.modules.knowledge.tasks.dispatch_drive_sync_outbox_event"
    )


def test_pending_outbox_sweeper_is_registered_for_restart_recovery() -> None:
    assert (
        dispatch_pending_drive_sync_outbox_events.name
        == "app.modules.knowledge.tasks.dispatch_pending_drive_sync_outbox_events"
    )


@pytest.mark.asyncio
async def test_outbox_delivery_dispatches_once_to_the_drive_sync_consumer(
    db_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    job_id = uuid4()
    event = OutboxEvent(
        event_type="knowledge.drive_source.sync.requested",
        aggregate_type="job",
        aggregate_id=job_id,
        payload={"source_id": str(uuid4())},
    )
    db_session.add(event)
    await db_session.flush()
    dispatched: list[str] = []

    def record_delay(job_id_value: str) -> None:
        dispatched.append(job_id_value)

    monkeypatch.setattr(drive_source_sync, "delay", record_delay)

    first = await _dispatch_drive_sync_outbox_event(event.event_id, db_session=db_session)
    second = await _dispatch_drive_sync_outbox_event(event.event_id, db_session=db_session)

    assert first is True
    assert second is False
    assert dispatched == [str(job_id)]


@pytest.mark.asyncio
async def test_broker_failure_leaves_outbox_event_pending_for_safe_redelivery(
    db_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    job_id = uuid4()
    event = OutboxEvent(
        event_type="knowledge.drive_source.sync.requested",
        aggregate_type="job",
        aggregate_id=job_id,
        payload={"source_id": str(uuid4())},
    )
    db_session.add(event)
    await db_session.commit()
    event_id = event.event_id

    def broker_down(_job_id_value: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(drive_source_sync, "delay", broker_down)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        await _dispatch_drive_sync_outbox_event(event_id, db_session=db_session)
    db_session.expire_all()
    pending = await db_session.get(OutboxEvent, event_id)
    assert pending is not None
    assert pending.published_at is None
    assert pending.publish_attempts == 1

    delivered: list[str] = []
    monkeypatch.setattr(drive_source_sync, "delay", delivered.append)
    assert await _dispatch_drive_sync_outbox_event(event_id, db_session=db_session) is True
    assert delivered == [str(job_id)]


@pytest.mark.asyncio
async def test_pending_outbox_sweeper_redelivers_after_post_commit_wakeup_loss(
    db_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    job_id = uuid4()
    event = OutboxEvent(
        event_type="knowledge.drive_source.sync.requested",
        aggregate_type="job",
        aggregate_id=job_id,
        payload={"source_id": str(uuid4())},
    )
    db_session.add(event)
    await db_session.commit()
    event_id = event.event_id

    delivered: list[str] = []
    monkeypatch.setattr(drive_source_sync, "delay", delivered.append)

    # This is the restart path: the request transaction committed, but no
    # post-commit broker wakeup was observed before the process disappeared.
    await _dispatch_pending_drive_sync_outbox_events(db_session=db_session)
    db_session.expire_all()
    persisted = await db_session.get(OutboxEvent, event_id)

    assert delivered == [str(job_id)]
    assert persisted is not None
    assert persisted.published_at is not None
    assert persisted.publish_attempts == 1


class _ConfigurationBoundary:
    configuration_resource_id = staticmethod(KnowledgeSourceService.configuration_resource_id)


@pytest.mark.asyncio
async def test_status_uses_durable_success_job_not_source_updated_at(db_session) -> None:  # type: ignore[no-untyped-def]
    organization = Organization(name="Sync status owner")
    db_session.add(organization)
    await db_session.flush()
    staff_user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"sync-status-{uuid4()}",
        email="status@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db_session.add(staff_user)
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="root",
        allowed_descendant_ids=[],
        connection_identity="reader@example.test",
        sync_cursor="cursor-2",
    )
    db_session.add(source)
    await db_session.flush()
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=staff_user.id,
            resource_type="knowledge",
            resource_id=KnowledgeSourceService.configuration_resource_id(organization.id),
            actions=["knowledge.write"],
        )
    )
    db_session.add(
        JobIntent(
            kind="knowledge.drive_source.sync",
            idempotency_key=drive_sync_job_key(source.id, "cursor-1"),
            payload={"source_id": str(source.id), "page_token": "cursor-1"},
            state="SUCCEEDED",
        )
    )
    await db_session.commit()

    result = await DriveSyncOperations(
        db_session, _ConfigurationBoundary()  # type: ignore[arg-type]
    ).status(
        principal=Principal(
            subject_id=staff_user.id,
            organization_id=organization.id,
            email=staff_user.email,
            role=staff_user.role,
            session_id=uuid4(),
            csrf_hash="csrf",
        ),
        source_id=source.id,
    )

    assert result.last_success_at is not None
    assert result.cursor == "cursor-2"
