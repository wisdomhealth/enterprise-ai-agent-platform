from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus
from app.modules.knowledge.drive_gateway import DriveConnection, DriveGateway
from app.modules.knowledge.models import DriveSource, KnowledgeBase
from app.modules.knowledge.service import KnowledgeSourceService


class _ConfigurationDriveClient:
    async def resolve_descendant_folder_ids(self, root_folder_id: str) -> set[str]:
        return {f"{root_folder_id}-child"}


class _ConfigurationDriveFactory:
    async def create(self, *, refresh_token: str) -> DriveConnection:
        assert refresh_token == "task21-refresh-token"
        return DriveConnection(
            gateway=DriveGateway(_ConfigurationDriveClient()),  # type: ignore[arg-type]
            connection_identity="drive-reader@example.test",
        )


@pytest_asyncio.fixture
async def operations_context(db_session: AsyncSession, tmp_path: Path) -> dict[str, object]:
    organization = Organization(name=f"Operations {uuid4()}")
    other_organization = Organization(name=f"Other {uuid4()}")
    db_session.add_all((organization, other_organization))
    await db_session.flush()
    connector_key_path = tmp_path / "connector.key"
    connector_key_path.write_bytes(b"t" * 32)
    connector_service = ConnectorService.for_file_key(
        connector_key_path,
        app_env="development",
    )
    admin = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"admin-{uuid4()}",
        email="admin@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    reviewer = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"reviewer-{uuid4()}",
        email="reviewer@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    foreign_admin = StaffUser(
        organization_id=other_organization.id,
        oidc_subject=f"foreign-{uuid4()}",
        email="foreign@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db_session.add_all((admin, reviewer, foreign_admin))
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="approved-root",
        allowed_descendant_ids=["approved-child"],
        connection_identity="drive-reader@example.test",
        sync_cursor="drive-cursor-9",
    )
    secret = await connector_service.store_refresh_token(
        db_session,
        organization_id=organization.id,
        refresh_token="task21-refresh-token",
    )
    db_session.add(source)
    await db_session.flush()
    connector = Connector(
        organization_id=organization.id,
        kind=ConnectorKind.DRIVE,
        status=ConnectorStatus.REAUTH_REQUIRED,
        secret_id=secret.id,
    )
    db_session.add(connector)
    await db_session.flush()
    db_session.add_all(
        (
            ResourceGrant(
                organization_id=organization.id,
                subject_id=admin.id,
                resource_type="knowledge",
                resource_id=KnowledgeSourceService.configuration_resource_id(organization.id),
                actions=["knowledge.write"],
            ),
            ResourceGrant(
                organization_id=organization.id,
                subject_id=admin.id,
                resource_type="connector",
                resource_id=connector.id,
                actions=["connector.reauthorize"],
            ),
        )
    )
    session = StaffSession(
        user_id=admin.id,
        csrf_hash="unused",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    await db_session.commit()

    def principal(user: StaffUser) -> Principal:
        return Principal(
            subject_id=user.id,
            organization_id=user.organization_id,
            email=user.email,
            role=user.role,
            session_id=session.id,
            csrf_hash="csrf",
        )

    @asynccontextmanager
    async def client_for(user: StaffUser) -> AsyncIterator[httpx.AsyncClient]:
        application = create_app(Settings.model_validate({"SESSION_SECRET": "task21-secret"}))
        application.state.connector_service = connector_service
        application.state.drive_gateway_factory = _ConfigurationDriveFactory()
        resolved_principal = principal(user)

        async def override_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        async def override_principal() -> Principal:
            return resolved_principal

        application.dependency_overrides[get_db_session] = override_db
        application.dependency_overrides[require_staff_session] = override_principal
        application.dependency_overrides[require_staff_csrf] = override_principal
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="https://testserver",
                headers={"X-CSRF-Token": "csrf"},
            ) as client:
                yield client
        finally:
            application.dependency_overrides.clear()

    return {
        "organization": organization,
        "other_organization": other_organization,
        "admin": admin,
        "reviewer": reviewer,
        "foreign_admin": foreign_admin,
        "source": source,
        "connector": connector,
        "session": session,
        "principal": principal,
        "client_for": client_for,
    }
