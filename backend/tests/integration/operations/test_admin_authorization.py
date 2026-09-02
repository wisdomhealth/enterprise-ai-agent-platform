import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditEvent
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.jobs.models import JobIntent, JobState
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
