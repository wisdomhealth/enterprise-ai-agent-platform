from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, status
from google.auth.exceptions import RefreshError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.knowledge.drive_gateway import DriveConnection, DriveFile, DriveGatewayFactory
from app.modules.knowledge.models import DriveSource, DriveSourceStatus, KnowledgeBase
from app.modules.knowledge.scope import DriveScope


@dataclass(frozen=True, slots=True)
class _ActiveDriveConnection:
    connection: DriveConnection
    connector_id: UUID
    secret_id: UUID


class DriveReauthorizationRequired(Exception):
    def __init__(self, *, connector_id: UUID, secret_id: UUID) -> None:
        super().__init__("Google Drive reauthorization is required")
        self.connector_id = connector_id
        self.secret_id = secret_id


class KnowledgeSourceService:
    def __init__(
        self,
        connector_service: ConnectorService,
        drive_gateway_factory: DriveGatewayFactory,
        *,
        audit_service: AuditService | None = None,
    ) -> None:
        self._connector_service = connector_service
        self._drive_gateway_factory = drive_gateway_factory
        self._audit_service = audit_service or AuditService()

    @staticmethod
    def configuration_resource_id(organization_id: UUID) -> UUID:
        return uuid5(NAMESPACE_URL, f"knowledge-source-configuration:{organization_id}")

    async def configure_drive_source(
        self,
        db_session: AsyncSession,
        *,
        principal: Principal,
        root_folder_id: str,
        include_descendants: bool = True,
    ) -> DriveSource:
        if principal.role is not UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        await self._require_configuration_authorization(db_session, principal)

        knowledge_base = await db_session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.organization_id == principal.organization_id)
        )
        if knowledge_base is None:
            knowledge_base = KnowledgeBase(organization_id=principal.organization_id)
            db_session.add(knowledge_base)
            await db_session.flush()

        source = await db_session.scalar(
            select(DriveSource).where(DriveSource.knowledge_base_id == knowledge_base.id)
        )
        previous_root_folder_ref = self._safe_reference(source.root_folder_id) if source else None
        previous_identity_ref = self._safe_reference(source.connection_identity) if source else None
        previous_include_descendants = source.include_descendants if source else None
        previous_connector_id = await self._previous_connector_id(db_session, source)
        active_connection = await self._load_drive_connection(db_session, principal.organization_id)
        try:
            descendant_ids = (
                await active_connection.connection.gateway.resolve_descendant_folder_ids(
                    root_folder_id
                )
                if include_descendants
                else set()
            )
        except Exception as error:
            self._raise_drive_reauthorization(
                error,
                active_connection.connector_id,
                active_connection.secret_id,
            )
            raise
        if source is None:
            source = DriveSource(
                organization_id=principal.organization_id,
                knowledge_base_id=knowledge_base.id,
                root_folder_id=root_folder_id,
                include_descendants=include_descendants,
                allowed_descendant_ids=sorted(descendant_ids),
                status=DriveSourceStatus.ACTIVE,
                connection_identity=active_connection.connection.connection_identity,
            )
            db_session.add(source)
        else:
            source.root_folder_id = root_folder_id
            source.include_descendants = include_descendants
            source.allowed_descendant_ids = sorted(descendant_ids)
            source.status = DriveSourceStatus.ACTIVE
            source.connection_identity = active_connection.connection.connection_identity
        await db_session.flush()
        root_folder_ref = self._safe_reference(source.root_folder_id)
        connection_identity_ref = self._safe_reference(source.connection_identity)
        await self._audit_service.record(
            db_session,
            principal,
            action="knowledge.drive_source.configure",
            object_type="drive_source",
            object_id=source.id,
            outcome="SUCCESS",
            details={
                "connector_id": str(active_connection.connector_id),
                "root_folder_ref": root_folder_ref,
                "connection_identity_ref": connection_identity_ref,
                "include_descendants": include_descendants,
                "changed_fields": {
                    "root_folder_ref": {
                        "before": previous_root_folder_ref,
                        "after": root_folder_ref,
                    },
                    "connection_identity_ref": {
                        "before": previous_identity_ref,
                        "after": connection_identity_ref,
                    },
                    "include_descendants": {
                        "before": previous_include_descendants,
                        "after": include_descendants,
                    },
                    "connector_id": {
                        "before": previous_connector_id,
                        "after": str(active_connection.connector_id),
                    },
                },
            },
            safe_detail_keys=(
                "connector_id",
                "root_folder_ref",
                "connection_identity_ref",
                "include_descendants",
                "changed_fields",
            ),
        )
        return source

    async def download_authorized(
        self, db_session: AsyncSession, *, source: DriveSource, file: DriveFile
    ) -> bytes:
        current_source = await db_session.scalar(
            select(DriveSource).where(
                DriveSource.id == source.id,
                DriveSource.organization_id == source.organization_id,
            )
        )
        if current_source is None or not self.is_file_authorized(current_source, file):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        active_connection = await self._load_drive_connection(
            db_session, current_source.organization_id
        )
        return await active_connection.connection.gateway.download(file.id)

    @staticmethod
    def is_file_authorized(source: DriveSource, file: DriveFile) -> bool:
        if source.status is not DriveSourceStatus.ACTIVE:
            return False
        return DriveScope(
            root_folder_id=source.root_folder_id,
            allowed_descendant_ids=set(source.allowed_descendant_ids),
        ).is_authorized(file)

    async def _require_configuration_authorization(
        self, db_session: AsyncSession, principal: Principal
    ) -> None:
        resource = ResourceRef(
            organization_id=principal.organization_id,
            resource_type="knowledge",
            resource_id=self.configuration_resource_id(principal.organization_id),
            state=ResourceState.ACTIVE,
        )
        try:
            await AuthorizationService(db_session).require(principal, "knowledge.write", resource)
        except AuthorizationDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc

    async def _load_drive_connection(
        self, db_session: AsyncSession, organization_id: UUID
    ) -> _ActiveDriveConnection:
        connector = await db_session.scalar(
            select(Connector).where(
                Connector.organization_id == organization_id,
                Connector.kind == ConnectorKind.DRIVE,
                Connector.status == ConnectorStatus.ACTIVE,
            )
        )
        if connector is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="an active Google Drive connector is required",
            )
        refresh_token = await self._connector_service.load_refresh_token(db_session, connector)
        try:
            connection = await self._drive_gateway_factory.create(refresh_token=refresh_token)
        except Exception as error:
            self._raise_drive_reauthorization(
                error,
                connector.id,
                connector.secret_id,
            )
            raise
        return _ActiveDriveConnection(connection, connector.id, connector.secret_id)

    async def mark_drive_reauthorization_required(
        self,
        db_session: AsyncSession,
        *,
        principal: Principal,
        error: DriveReauthorizationRequired,
    ) -> bool:
        return await self._connector_service.mark_drive_reauthorization_required(
            db_session,
            principal=principal,
            connector_id=error.connector_id,
            expected_secret_id=error.secret_id,
        )

    @staticmethod
    def _raise_drive_reauthorization(
        error: Exception,
        connector_id: UUID,
        secret_id: UUID,
    ) -> None:
        http_status = getattr(getattr(error, "resp", None), "status", None)
        invalid_grant = False
        if isinstance(error, RefreshError):
            response = next((arg for arg in error.args if isinstance(arg, Mapping)), None)
            invalid_grant = (
                response is not None and response.get("error") == "invalid_grant"
            ) or any(
                isinstance(arg, str) and "invalid_grant" in arg.lower()
                for arg in error.args
            )
        if http_status == 401 or invalid_grant:
            raise DriveReauthorizationRequired(
                connector_id=connector_id,
                secret_id=secret_id,
            ) from error

    @staticmethod
    def _safe_reference(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    async def _previous_connector_id(
        db_session: AsyncSession, source: DriveSource | None
    ) -> str | None:
        if source is None:
            return None
        previous_root_folder_ref = KnowledgeSourceService._safe_reference(source.root_folder_id)
        previous_identity_ref = KnowledgeSourceService._safe_reference(source.connection_identity)
        previous_events = (
            await db_session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == source.organization_id,
                AuditEvent.object_id == source.id,
                AuditEvent.action == "knowledge.drive_source.configure",
            )
            )
        ).all()
        matching_connector_ids: set[str] = set()
        for event in previous_events:
            connector_id = event.details.get("connector_id")
            if (
                event.details.get("root_folder_ref") == previous_root_folder_ref
                and event.details.get("connection_identity_ref") == previous_identity_ref
                and isinstance(connector_id, str)
            ):
                matching_connector_ids.add(connector_id)
        if len(matching_connector_ids) != 1:
            return None
        return matching_connector_ids.pop()
