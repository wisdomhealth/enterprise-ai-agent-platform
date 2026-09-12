import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.database import async_sessionmaker
from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import ConnectorKind
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.operations.connector_authorization import ConnectorAuthorizationService


@pytest.mark.asyncio
async def test_non_admin_cannot_discover_connector_grant_management(
    operations_context,
) -> None:  # type: ignore[no-untyped-def]
    reviewer = operations_context["reviewer"]
    async with operations_context["client_for"](reviewer) as client:
        response = await client.get("/api/v1/admin/authorization/connectors")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_lists_fixed_connector_grants_without_connector_grant(
    operations_context,
) -> None:  # type: ignore[no-untyped-def]
    async with operations_context["client_for"](operations_context["admin"]) as client:
        response = await client.get("/api/v1/admin/authorization/connectors")

    assert response.status_code == 200
    entries = {item["kind"]: item for item in response.json()}
    assert set(entries) == {"DRIVE", "GMAIL"}
    assert entries["DRIVE"]["resource_id"] == str(
        ConnectorService.configuration_resource_id(
            operations_context["organization"].id, ConnectorKind.DRIVE
        )
    )
    assert entries["DRIVE"]["actions"] == [
        {"action": "connector.create", "granted": False},
        {"action": "connector.reauthorize", "granted": False},
        {"action": "connector.revoke", "granted": False},
    ]


@pytest.mark.asyncio
async def test_admin_replaces_only_server_derived_connector_actions_and_audits(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    admin = operations_context["admin"]
    organization = operations_context["organization"]
    organization_id = organization.id
    async with operations_context["client_for"](admin) as client:
        response = await client.put(
            "/api/v1/admin/authorization/connectors/GMAIL/grants",
            headers={"Idempotency-Key": "gmail-grant-update"},
            json={
                "staff_user_id": str(admin.id),
                "actions": ["connector.create", "connector.revoke"],
            },
        )

    assert response.status_code == 200
    gmail_resource = ConnectorService.configuration_resource_id(
        organization_id, ConnectorKind.GMAIL
    )
    grant = await db_session.scalar(
        select(ResourceGrant).where(
                ResourceGrant.organization_id == organization_id,
            ResourceGrant.subject_id == admin.id,
            ResourceGrant.resource_id == gmail_resource,
        )
    )
    assert grant is not None
    assert grant.actions == ["connector.create", "connector.revoke"]
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "authorization.connector_grants.update",
            AuditEvent.organization_id == organization_id,
            AuditEvent.object_id == gmail_resource,
        )
    )
    assert audit is not None
    assert audit.details["connector_kind"] == "GMAIL"
    assert audit.details["resource_id"] == str(gmail_resource)
    assert audit.details["added_actions"] == ["connector.create", "connector.revoke"]


@pytest.mark.asyncio
async def test_cross_organization_target_and_unsupported_actions_are_not_mutable(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    admin = operations_context["admin"]
    foreign_admin = operations_context["foreign_admin"]
    admin_id = admin.id
    foreign_admin_id = foreign_admin.id
    other_organization_id = operations_context["other_organization"].id
    async with operations_context["client_for"](admin) as client:
        cross_org = await client.put(
            "/api/v1/admin/authorization/connectors/DRIVE/grants",
            headers={"Idempotency-Key": "foreign-target"},
            json={"staff_user_id": str(foreign_admin_id), "actions": ["connector.create"]},
        )
        unsupported = await client.put(
            "/api/v1/admin/authorization/connectors/DRIVE/grants",
            headers={"Idempotency-Key": f"unsupported-{uuid4()}"},
            json={"staff_user_id": str(admin_id), "actions": ["knowledge.read"]},
        )

    assert cross_org.status_code == 404
    assert unsupported.status_code == 422
    foreign_drive_resource = ConnectorService.configuration_resource_id(
        other_organization_id, ConnectorKind.DRIVE
    )
    assert await db_session.scalar(
        select(ResourceGrant).where(
            ResourceGrant.subject_id == foreign_admin_id,
            ResourceGrant.resource_id == foreign_drive_resource,
        )
    ) is None


@pytest.mark.asyncio
async def test_idempotent_revoke_removes_only_intended_connector_actions(
    db_session, operations_context
) -> None:  # type: ignore[no-untyped-def]
    admin = operations_context["admin"]
    organization = operations_context["organization"]
    drive_resource = ConnectorService.configuration_resource_id(
        organization.id, ConnectorKind.DRIVE
    )
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=admin.id,
            resource_type="connector",
            resource_id=drive_resource,
            actions=["connector.create", "connector.reauthorize", "connector.revoke"],
        )
    )
    await db_session.commit()
    async with operations_context["client_for"](admin) as client:
        first = await client.put(
            "/api/v1/admin/authorization/connectors/DRIVE/grants",
            headers={"Idempotency-Key": "revoke-only"},
            json={"staff_user_id": str(admin.id), "actions": ["connector.create"]},
        )
        replay = await client.put(
            "/api/v1/admin/authorization/connectors/DRIVE/grants",
            headers={"Idempotency-Key": "revoke-only"},
            json={"staff_user_id": str(admin.id), "actions": ["connector.create"]},
        )

    assert first.status_code == replay.status_code == 200
    grant = await db_session.scalar(
        select(ResourceGrant).where(ResourceGrant.resource_id == drive_resource)
    )
    assert grant is not None
    assert grant.actions == ["connector.create"]


@pytest.mark.asyncio
async def test_concurrent_first_grant_update_serializes_on_target_administrator() -> None:
    async with async_sessionmaker() as setup_session:
        organization = Organization(name=f"Concurrent grant {uuid4()}")
        setup_session.add(organization)
        await setup_session.flush()
        administrator = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"concurrent-admin-{uuid4()}",
            email=f"concurrent-{uuid4()}@example.test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        setup_session.add(administrator)
        await setup_session.commit()
        organization_id = organization.id
        administrator_id = administrator.id

    principal = Principal(
        subject_id=administrator_id,
        organization_id=organization_id,
        email="concurrent@example.test",
        role=UserRole.ADMIN,
        session_id=uuid4(),
        csrf_hash="csrf",
    )

    async def replace(actions: list[str]) -> None:
        async with async_sessionmaker() as worker_session:
            await ConnectorAuthorizationService(worker_session).replace(
                principal,
                target_staff_user_id=administrator_id,
                kind=ConnectorKind.GMAIL,
                actions=actions,
            )
            await worker_session.commit()

    try:
        await asyncio.gather(
            replace(["connector.create"]),
            replace(["connector.create", "connector.reauthorize"]),
        )
        async with async_sessionmaker() as verify_session:
            grant = await verify_session.scalar(
                select(ResourceGrant).where(
                    ResourceGrant.organization_id == organization_id,
                    ResourceGrant.subject_id == administrator_id,
                    ResourceGrant.resource_id
                    == ConnectorService.configuration_resource_id(
                        organization_id, ConnectorKind.GMAIL
                    ),
                )
            )
        assert grant is not None
        assert len(grant.actions) in {1, 2}
    finally:
        async with async_sessionmaker() as cleanup_session:
            organization = await cleanup_session.get(Organization, organization_id)
            if organization is not None:
                await cleanup_session.delete(organization)
                await cleanup_session.commit()
