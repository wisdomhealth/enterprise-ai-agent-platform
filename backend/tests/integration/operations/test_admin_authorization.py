import pytest
from sqlalchemy import delete, select

from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.email.models import EmailEvaluationRun
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.outbox.models import OutboxEvent


@pytest.mark.asyncio
async def test_reviewer_cannot_open_admin_failures(operations_context) -> None:  # type: ignore[no-untyped-def]
    async with operations_context["client_for"](operations_context["reviewer"]) as client:
        response = await client.get("/api/v1/admin/jobs/failed")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reviewer_cannot_discover_admin_summary_or_mutations(operations_context) -> None:  # type: ignore[no-untyped-def]
    connector = operations_context["connector"]
    reviewer = operations_context["reviewer"]
    async with operations_context["client_for"](reviewer) as client:
        summary = await client.get("/api/v1/admin/operations/summary")
        reauthorize = await client.post(
            f"/api/v1/admin/connectors/{connector.id}/reauthorize",
            headers={"Idempotency-Key": "reviewer-reauthorize"},
        )
        invitation = await client.post(
            "/api/v1/admin/users/invitations",
            headers={"Idempotency-Key": "reviewer-invite"},
            json={"email": "new@example.com", "role": "MEMBER"},
        )

    assert summary.status_code == 404
    assert reauthorize.status_code == 404
    assert invitation.status_code == 404


@pytest.mark.asyncio
async def test_admin_cannot_probe_unowned_operational_resources(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    unowned_job = JobIntent(
        kind="unrelated.internal.job",
        idempotency_key="unowned-operation-probe",
        payload={},
        state=JobState.FAILED,
    )
    ungranted_connector = Connector(
        organization_id=operations_context["organization"].id,
        kind=ConnectorKind.GMAIL,
        status=ConnectorStatus.REAUTH_REQUIRED,
        secret_id=operations_context["connector"].secret_id,
    )
    db_session.add_all((unowned_job, ungranted_connector))
    await db_session.commit()
    unowned_job_id = unowned_job.id
    ungranted_connector_id = ungranted_connector.id

    async with operations_context["client_for"](operations_context["admin"]) as client:
        job_response = await client.post(
            f"/api/v1/admin/jobs/{unowned_job_id}/retry",
            headers={"Idempotency-Key": "unowned-job-probe"},
        )
        connector_response = await client.post(
            f"/api/v1/admin/connectors/{ungranted_connector_id}/reauthorize",
            headers={"Idempotency-Key": "ungranted-connector-probe"},
        )

    assert job_response.status_code == 404
    assert connector_response.status_code == 404


@pytest.mark.asyncio
async def test_admin_read_projections_hide_resources_without_current_grants(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    organization = operations_context["organization"]
    source = operations_context["source"]
    existing_connector = operations_context["connector"]
    ungranted_connector = Connector(
        organization_id=organization.id,
        kind=ConnectorKind.GMAIL,
        status=ConnectorStatus.REAUTH_REQUIRED,
        secret_id=existing_connector.secret_id,
    )
    ungranted_job = JobIntent(
        kind="knowledge.drive_source.sync",
        idempotency_key="ungranted-drive-failure",
        payload={"source_id": str(source.id)},
        state=JobState.FAILED,
    )
    global_email_quality = EmailEvaluationRun(
        dataset_version="task21-review",
        dataset_kind="regression",
        dataset_digest="1" * 64,
        model="safe-model-id",
        prompt_version="safe-prompt-id",
        macro_f1=0.9,
        structured_output_success=1.0,
        latency_ms=10,
        input_tokens=1,
        output_tokens=1,
        estimated_cost=0.01,
    )
    source_id = source.id

    async with operations_context["client_for"](operations_context["admin"]) as client:
        db_session.add_all((ungranted_connector, ungranted_job, global_email_quality))
        await db_session.commit()
        connector_id = str(ungranted_connector.id)
        ungranted_job_id = ungranted_job.id

        with_grant = await client.get("/api/v1/admin/operations/summary")

        await db_session.execute(
            delete(ResourceGrant).where(
                ResourceGrant.organization_id == organization.id,
                ResourceGrant.subject_id == operations_context["admin"].id,
                ResourceGrant.resource_type == "knowledge",
                ResourceGrant.resource_id
                == KnowledgeSourceService.configuration_resource_id(organization.id),
            )
        )
        await db_session.commit()
        without_grant = await client.get("/api/v1/admin/operations/summary")
        failures = await client.get("/api/v1/admin/jobs/failed")
        retry = await client.post(
            f"/api/v1/admin/jobs/{ungranted_job_id}/retry",
            headers={"Idempotency-Key": "ungranted-drive-retry"},
        )
        configure = await client.put(
            "/api/v1/admin/knowledge-sources/drive",
            json={"root_folder_id": "must-not-disclose", "include_descendants": True},
        )
        sync = await client.post(f"/api/v1/admin/knowledge-sources/{source_id}/sync")
        status_response = await client.get(f"/api/v1/admin/knowledge-sources/{source_id}/status")

    assert with_grant.status_code == 200
    assert connector_id not in {entry["id"] for entry in with_grant.json()["connectors"]}
    assert without_grant.status_code == 200
    assert without_grant.json()["knowledge_sources"] == []
    assert without_grant.json()["email_quality"] is None
    assert failures.status_code == 200
    assert str(ungranted_job_id) not in {entry["job_id"] for entry in failures.json()}
    assert retry.status_code == 404
    assert configure.status_code == 404
    assert sync.status_code == 404
    assert status_response.status_code == 404


@pytest.mark.asyncio
async def test_admin_reauthorization_uses_existing_oauth_path_and_safe_evidence(
    operations_context, db_session
) -> None:  # type: ignore[no-untyped-def]
    connector = operations_context["connector"]
    async with operations_context["client_for"](operations_context["admin"]) as client:
        response = await client.post(
            f"/api/v1/admin/connectors/{connector.id}/reauthorize",
            headers={"Idempotency-Key": "admin-reauthorize-drive"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "connector_id": str(connector.id),
        "authorization_url": "/api/v1/admin/connectors/DRIVE/authorize",
        "requested_scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    }
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "connector.reauthorization.start",
            AuditEvent.object_id == connector.id,
        )
    )
    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "connector.reauthorization.started",
            OutboxEvent.aggregate_id == connector.id,
        )
    )
    assert audit is not None
    assert audit.details == {"kind": "DRIVE", "status": "REAUTH_REQUIRED"}
    assert event is not None
    evidence = f"{audit.details!r}{event.payload!r}".lower()
    assert "refresh_token" not in evidence
    assert "access_token" not in evidence
    assert "client_secret" not in evidence
