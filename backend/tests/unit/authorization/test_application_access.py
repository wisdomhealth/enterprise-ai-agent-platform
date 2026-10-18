from uuid import uuid4

from app.modules.authorization.application import (
    ApplicationCapability,
    application_access_resource,
    application_access_resource_id,
)
from app.modules.authorization.policy import ACTION_STATES, ROLE_ACTIONS
from app.modules.authorization.types import ResourceState
from app.modules.identity.models import UserRole


def test_application_resource_ids_are_deterministic_and_tenant_scoped() -> None:
    organization_id = uuid4()
    other_organization_id = uuid4()

    email_id = application_access_resource_id(
        organization_id,
        ApplicationCapability.EMAIL,
    )

    assert email_id == application_access_resource_id(
        organization_id,
        ApplicationCapability.EMAIL,
    )
    assert email_id != application_access_resource_id(
        organization_id,
        ApplicationCapability.SUPPORT,
    )
    assert email_id != application_access_resource_id(
        other_organization_id,
        ApplicationCapability.EMAIL,
    )


def test_application_access_is_active_grant_gated_for_admin_and_reviewer() -> None:
    organization_id = uuid4()
    resource = application_access_resource(
        organization_id,
        ApplicationCapability.EMAIL,
    )

    assert resource.organization_id == organization_id
    assert resource.resource_type == "application"
    assert resource.state is ResourceState.ACTIVE
    assert resource.is_public is False
    assert "application.access" in ROLE_ACTIONS[UserRole.ADMIN]
    assert "application.access" in ROLE_ACTIONS[UserRole.REVIEWER]
    assert "application.access" not in ROLE_ACTIONS[UserRole.MEMBER]
    assert ACTION_STATES["application.access"] == frozenset({ResourceState.ACTIVE})
