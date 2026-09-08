from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.celery import create_celery
from app.core.database import async_sessionmaker, engine
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobService
from app.modules.knowledge import tasks as knowledge_tasks
from app.modules.knowledge.drive_gateway import (
    DriveConnection,
    DriveGateway,
    GoogleDriveGatewayFactory,
)
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DriveSource,
    KnowledgeBase,
)
from app.modules.knowledge.tasks import (
    _dispatch_document_parse_outbox_event,
    _dispatch_pending_document_parse_outbox_events,
    dispatch_document_parse_outbox_event,
    dispatch_pending_document_parse_outbox_events,
    document_parse,
)
from app.modules.outbox.models import OutboxEvent


class _FakeDriveGateway(DriveGateway):
    def __init__(self, content: bytes) -> None:
        super().__init__()
        self.download_calls: list[str] = []
        self._content = content

    async def download(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        return self._content


class _FakeDriveGatewayFactory:
    def __init__(self, gateway: _FakeDriveGateway) -> None:
        self._gateway = gateway

    async def create(self, *, refresh_token: str) -> DriveConnection:
        return DriveConnection(gateway=self._gateway, connection_identity="reader@example.test")


class _EmbeddingProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 1536 for _ in texts]


async def _successful_parent_sync_job(db_session) -> JobIntent:  # type: ignore[no-untyped-def]
    job = await JobService().enqueue(
        db_session,
        "knowledge.drive_source.sync",
        f"document-parse-parent-sync-{uuid4()}",
        {"source_id": str(uuid4())},
    )
    job.state = JobState.SUCCEEDED
    await db_session.flush()
    return job


async def _parse_job_fixture(
    db_session, tmp_path: Path, *, mime_type: str
) -> tuple[JobIntent, ConnectorService, _FakeDriveGateway]:  # type: ignore[no-untyped-def]
    organization = Organization(name=f"parse task {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="source-root",
        allowed_descendant_ids=["authorized-folder"],
        connection_identity="reader@example.test",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id="drive-file-1",
        title="Customer policy",
        mime_type=mime_type,
    )
    db_session.add(document)
    await db_session.flush()
    key_path = tmp_path / "connector-master-key"
    key_path.write_bytes(b"k" * 32)
    connector_service = ConnectorService.for_file_key(key_path, app_env="development")
    secret = await connector_service.store_refresh_token(
        db_session,
        organization_id=organization.id,
        refresh_token="test-only-refresh-token",
    )
    db_session.add(
        Connector(
            organization_id=organization.id,
            kind=ConnectorKind.DRIVE,
            status=ConnectorStatus.ACTIVE,
            secret_id=secret.id,
        )
    )
    job = await JobService().enqueue(
        db_session,
        "knowledge.document.parse",
        f"document-parse-task-{uuid4()}",
        {
            "document_id": str(document.id),
            "drive_file": {
                "id": "drive-file-1",
                "name": "policy.pdf",
                "mime_type": mime_type,
                "modified_time": datetime.now(UTC).isoformat(),
                "parent_ids": ["authorized-folder"],
                "web_view_link": None,
                "removed": False,
            },
        },
    )
    await db_session.commit()
    content_name = "sample.pdf" if mime_type == "application/pdf" else "sample.docx"
    gateway = _FakeDriveGateway((Path("tests/fixtures/documents") / content_name).read_bytes())
    return job, connector_service, gateway


def test_document_parse_consumer_is_registered() -> None:
    assert document_parse.name == "app.modules.knowledge.tasks.document_parse"


def test_document_parse_consumer_forwards_the_durable_job_id_to_the_authorized_pipeline(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    job_id = uuid4()
    consumed: list[object] = []

    async def record_consumer(received_job_id):  # type: ignore[no-untyped-def]
        consumed.append(received_job_id)

    monkeypatch.setattr(knowledge_tasks, "_consume_document_parse_intent", record_consumer)

    document_parse.run(str(job_id))

    assert consumed == [job_id]


def test_document_parse_outbox_dispatcher_is_registered() -> None:
    assert (
        dispatch_document_parse_outbox_event.name
        == "app.modules.knowledge.tasks.dispatch_document_parse_outbox_event"
    )


def test_document_parse_pending_outbox_sweeper_is_registered() -> None:
    assert (
        dispatch_pending_document_parse_outbox_events.name
        == "app.modules.knowledge.tasks.dispatch_pending_document_parse_outbox_events"
    )


def test_document_parse_outbox_sweeper_is_scheduled_for_restart_recovery() -> None:
    assert create_celery().conf.beat_schedule["knowledge-document-parse-outbox-dispatch"] == {
        "task": ("app.modules.knowledge.tasks.dispatch_pending_document_parse_outbox_events"),
        "schedule": 60,
    }


@pytest.mark.asyncio
async def test_document_parse_outbox_dispatches_once_and_publishes_after_broker_acceptance(
    db_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    job_id = uuid4()
    parent_job = await _successful_parent_sync_job(db_session)
    event = OutboxEvent(
        event_type="knowledge.document.parse.requested",
        aggregate_type="job",
        aggregate_id=job_id,
        payload={
            "document_id": str(uuid4()),
            "parent_sync_job_id": str(parent_job.id),
        },
    )
    db_session.add(event)
    await db_session.flush()
    delivered: list[str] = []
    monkeypatch.setattr(document_parse, "delay", delivered.append)

    assert await _dispatch_document_parse_outbox_event(event.event_id, db_session=db_session)
    assert not await _dispatch_document_parse_outbox_event(event.event_id, db_session=db_session)

    await db_session.refresh(event)
    assert delivered == [str(job_id)]
    assert event.published_at is not None
    assert event.publish_attempts == 1


@pytest.mark.asyncio
async def test_document_parse_broker_failure_leaves_event_unpublished_for_recovery(
    db_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    parent_job = await _successful_parent_sync_job(db_session)
    event = OutboxEvent(
        event_type="knowledge.document.parse.requested",
        aggregate_type="job",
        aggregate_id=uuid4(),
        payload={
            "document_id": str(uuid4()),
            "parent_sync_job_id": str(parent_job.id),
        },
    )
    db_session.add(event)
    await db_session.commit()

    def broker_down(_job_id: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(document_parse, "delay", broker_down)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        await _dispatch_document_parse_outbox_event(event.event_id, db_session=db_session)

    await db_session.refresh(event)
    assert event.published_at is None
    assert event.publish_attempts == 1


@pytest.mark.asyncio
async def test_document_parse_pending_sweeper_recovers_unpublished_events(
    db_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    job_id = uuid4()
    parent_job = await _successful_parent_sync_job(db_session)
    event = OutboxEvent(
        event_type="knowledge.document.parse.requested",
        aggregate_type="job",
        aggregate_id=job_id,
        payload={
            "document_id": str(uuid4()),
            "parent_sync_job_id": str(parent_job.id),
        },
    )
    db_session.add(event)
    await db_session.commit()
    delivered: list[str] = []
    monkeypatch.setattr(document_parse, "delay", delivered.append)

    await _dispatch_pending_document_parse_outbox_events(db_session=db_session)

    await db_session.refresh(event)
    assert delivered == [str(job_id)]
    assert event.published_at is not None
    assert event.publish_attempts == 1


@pytest.mark.asyncio
async def test_document_parse_sweeper_waits_for_its_parent_sync_job_to_succeed(
    db_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    service = JobService()
    parent_job = await service.enqueue(
        db_session,
        "knowledge.drive_source.sync",
        f"document-parse-parent-sync-{uuid4()}",
        {"source_id": str(uuid4())},
    )
    parse_job = await service.enqueue(
        db_session,
        "knowledge.document.parse",
        f"document-parse-child-{uuid4()}",
        {"document_id": str(uuid4())},
    )
    event = OutboxEvent(
        event_type="knowledge.document.parse.requested",
        aggregate_type="job",
        aggregate_id=parse_job.id,
        payload={
            "document_id": str(uuid4()),
            "parent_sync_job_id": str(parent_job.id),
        },
    )
    db_session.add(event)
    await db_session.commit()
    delivered: list[str] = []
    monkeypatch.setattr(document_parse, "delay", delivered.append)

    await _dispatch_pending_document_parse_outbox_events(db_session=db_session)

    await db_session.refresh(event)
    assert delivered == []
    assert event.published_at is None
    assert event.publish_attempts == 0

    parent_job.state = JobState.SUCCEEDED
    await db_session.commit()
    await _dispatch_pending_document_parse_outbox_events(db_session=db_session)

    await db_session.refresh(event)
    assert delivered == [str(parse_job.id)]
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_legacy_document_parse_event_recovers_after_matching_sync_succeeds(
    db_session, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Pre-parent-provenance events remain recoverable only after their source sync succeeds."""
    parse_job, _, _ = await _parse_job_fixture(
        db_session, tmp_path, mime_type="application/pdf"
    )
    document_id = UUID(str(parse_job.payload["document_id"]))
    document = await db_session.get(Document, document_id)
    assert document is not None
    parent_job = await JobService().enqueue(
        db_session,
        "knowledge.drive_source.sync",
        f"legacy-document-parse-parent-{uuid4()}",
        {"source_id": str(document.source_id)},
    )
    event = OutboxEvent(
        event_type="knowledge.document.parse.requested",
        aggregate_type="job",
        aggregate_id=parse_job.id,
        payload={
            "document_id": str(document.id),
            "source_id": str(document.source_id),
        },
    )
    db_session.add(event)
    await db_session.commit()
    delivered: list[str] = []
    monkeypatch.setattr(document_parse, "delay", delivered.append)

    await _dispatch_pending_document_parse_outbox_events(db_session=db_session)
    await db_session.refresh(event)
    assert delivered == []
    assert event.published_at is None

    parent_job.state = JobState.SUCCEEDED
    await db_session.commit()
    await _dispatch_pending_document_parse_outbox_events(db_session=db_session)

    await db_session.refresh(event)
    assert delivered == [str(parse_job.id)]
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_legacy_document_parse_event_rejects_a_different_source_sync(
    db_session, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    parse_job, _, _ = await _parse_job_fixture(
        db_session, tmp_path, mime_type="application/pdf"
    )
    document_id = UUID(str(parse_job.payload["document_id"]))
    document = await db_session.get(Document, document_id)
    assert document is not None
    unrelated_sync = await JobService().enqueue(
        db_session,
        "knowledge.drive_source.sync",
        f"legacy-document-parse-unrelated-{uuid4()}",
        {"source_id": str(uuid4())},
    )
    event = OutboxEvent(
        event_type="knowledge.document.parse.requested",
        aggregate_type="job",
        aggregate_id=parse_job.id,
        payload={
            "document_id": str(document.id),
            "source_id": str(document.source_id),
        },
    )
    db_session.add(event)
    await db_session.commit()
    unrelated_sync.state = JobState.SUCCEEDED
    await db_session.commit()
    delivered: list[str] = []
    monkeypatch.setattr(document_parse, "delay", delivered.append)

    await _dispatch_pending_document_parse_outbox_events(db_session=db_session)

    await db_session.refresh(event)
    assert delivered == []
    assert event.published_at is None
    assert event.publish_attempts == 0


@pytest.mark.asyncio
async def test_document_parse_event_with_null_parent_provenance_fails_closed(
    db_session, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    parse_job, _, _ = await _parse_job_fixture(
        db_session, tmp_path, mime_type="application/pdf"
    )
    document_id = UUID(str(parse_job.payload["document_id"]))
    document = await db_session.get(Document, document_id)
    assert document is not None
    matching_sync = await JobService().enqueue(
        db_session,
        "knowledge.drive_source.sync",
        f"null-parent-document-parse-sync-{uuid4()}",
        {"source_id": str(document.source_id)},
    )
    event = OutboxEvent(
        event_type="knowledge.document.parse.requested",
        aggregate_type="job",
        aggregate_id=parse_job.id,
        payload={
            "document_id": str(document.id),
            "source_id": str(document.source_id),
            "parent_sync_job_id": None,
        },
    )
    db_session.add(event)
    await db_session.commit()
    matching_sync.state = JobState.SUCCEEDED
    await db_session.commit()
    delivered: list[str] = []
    monkeypatch.setattr(document_parse, "delay", delivered.append)

    await _dispatch_pending_document_parse_outbox_events(db_session=db_session)

    await db_session.refresh(event)
    assert delivered == []
    assert event.published_at is None
    assert event.publish_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type",
    [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
)
async def test_document_parse_celery_consumer_uses_authorized_pipeline_once_per_job(
    monkeypatch, tmp_path, mime_type
) -> None:  # type: ignore[no-untyped-def]
    try:
        async with async_sessionmaker() as setup_session:
            job, connector_service, gateway = await _parse_job_fixture(
                setup_session,
                tmp_path,
                mime_type=mime_type,
            )
        gateway_factory = _FakeDriveGatewayFactory(gateway)
        monkeypatch.setattr(
            ConnectorService,
            "from_settings",
            classmethod(lambda _cls, _settings: connector_service),
        )
        monkeypatch.setattr(
            GoogleDriveGatewayFactory,
            "from_settings",
            classmethod(lambda _cls, _settings: gateway_factory),
        )
        from app.modules.rag.embeddings import OpenAIEmbeddingProvider

        monkeypatch.setattr(
            OpenAIEmbeddingProvider,
            "from_settings",
            classmethod(lambda _cls, _settings: _EmbeddingProvider()),
        )

        await knowledge_tasks._consume_document_parse_intent(job.id)
        await knowledge_tasks._consume_document_parse_intent(job.id)

        async with async_sessionmaker() as inspection_session:
            persisted_job = await inspection_session.get(JobIntent, job.id)
            assert persisted_job is not None
            assert persisted_job.state is JobState.SUCCEEDED
            version_id = persisted_job.payload["document_version_id"]
            assert isinstance(version_id, str)
            version = await inspection_session.get(DocumentVersion, UUID(version_id))
            assert version is not None
            versions = (
                await inspection_session.scalars(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == version.document_id
                    )
                )
            ).all()
            chunks = (
                await inspection_session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.document_version_id == UUID(version_id)
                    )
                )
            ).all()
        assert gateway.download_calls == ["drive-file-1"]
        assert len(versions) == 1
        assert chunks
    finally:
        await engine.dispose()
