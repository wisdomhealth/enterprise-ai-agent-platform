from uuid import UUID

import pytest
from sqlalchemy import select

from app.modules.authorization.models import ResourceGrant
from app.modules.authorization.policy import resource_grant_filter
from app.modules.identity.dependencies import Principal


@pytest.mark.asyncio
async def test_cross_organization_resource_returns_not_found(client, authorization_records):
    client.cookies.set("staff_session", authorization_records["staff_cookie"])
    response = await client.get(
        f"/api/v1/authorization-probe/{authorization_records['foreign_resource_id']}"
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unassigned_same_organization_resource_returns_not_found(
    client,
    authorization_records,
):
    client.cookies.set("staff_session", authorization_records["staff_cookie"])
    response = await client.get(
        f"/api/v1/authorization-probe/{authorization_records['other_subject_resource_id']}"
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_visible_resource_with_forbidden_action_returns_forbidden(
    client,
    authorization_records,
):
    client.cookies.set("staff_session", authorization_records["staff_cookie"])
    response = await client.get(
        f"/api/v1/authorization-probe/{authorization_records['forbidden_resource_id']}"
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_assigned_resource_with_allowed_action_is_returned(client, authorization_records):
    resource_id = authorization_records["readable_resource_id"]
    client.cookies.set("staff_session", authorization_records["staff_cookie"])
    response = await client.get(f"/api/v1/authorization-probe/{resource_id}")

    assert response.status_code == 200
    assert response.json() == {"resource_id": str(resource_id)}


@pytest.mark.asyncio
async def test_resource_candidate_filter_excludes_other_subjects_and_organizations(
    db_session,
    authorization_records,
):
    staff_user = authorization_records["staff_user"]
    principal = Principal(
        subject_id=staff_user.id,
        organization_id=staff_user.organization_id,
        email=staff_user.email,
        role=staff_user.role,
        session_id=UUID(int=0),
        csrf_hash="test",
    )

    resource_ids = set(
        await db_session.scalars(
            select(ResourceGrant.resource_id).where(resource_grant_filter(principal))
        )
    )

    assert resource_ids == {
        authorization_records["readable_resource_id"],
        authorization_records["forbidden_resource_id"],
    }


@pytest.mark.asyncio
async def test_unfiltered_loader_does_not_reveal_unassigned_resource_exists(
    client,
    authorization_records,
):
    client.cookies.set("staff_session", authorization_records["staff_cookie"])

    response = await client.get(
        "/api/v1/authorization-probe/unfiltered/"
        f"{authorization_records['other_subject_resource_id']}"
    )

    assert response.status_code == 404
