from dataclasses import dataclass
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.connectors.encryption import EncryptedSecret
from app.modules.connectors.models import (
    Connector,
    ConnectorKind,
    ConnectorSecret,
    ConnectorStatus,
)
from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.knowledge.drive_gateway import (
    DriveFile,
    DriveFileUnavailable,
    DriveGatewayFactory,
    is_drive_authorization_error,
)
from app.modules.knowledge.models import DriveSource, DriveSourceStatus, KnowledgeBase
from app.modules.knowledge.scope import DriveScope

DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


@dataclass(frozen=True, slots=True)
class _ConnectorCredentialSnapshot:
    connector_id: UUID
    connector_secret_id: UUID
    encrypted_secret: EncryptedSecret


@dataclass(frozen=True, slots=True)
class _SourceConfigurationSnapshot:
    source_id: UUID | None
    knowledge_base_id: UUID | None
    root_folder_id: str | None
    include_descendants: bool | None
    allowed_descendant_ids: tuple[str, ...]
    sync_cursor: str | None
    status: DriveSourceStatus | None
    connection_identity: str | None


@dataclass(frozen=True, slots=True)
class _DriveDownloadSnapshot:
    source_id: UUID
    organization_id: UUID
    root_folder_id: str
    include_descendants: bool
    allowed_descendant_ids: tuple[str, ...]
    status: DriveSourceStatus
    connection_identity: str
    connector_id: UUID
    connector_secret_id: UUID
    encrypted_secret: EncryptedSecret


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
        source = await db_session.scalar(
            select(DriveSource).where(
                DriveSource.organization_id == principal.organization_id
            )
        )
        previous_root_folder_ref = self._safe_reference(source.root_folder_id) if source else None
        previous_identity_ref = self._safe_reference(source.connection_identity) if source else None
        previous_include_descendants = source.include_descendants if source else None
        previous_connector_id = await self._previous_connector_id(db_session, source)
        source_snapshot = _SourceConfigurationSnapshot(
            source_id=source.id if source is not None else None,
            knowledge_base_id=knowledge_base.id if knowledge_base is not None else None,
            root_folder_id=source.root_folder_id if source is not None else None,
            include_descendants=source.include_descendants if source is not None else None,
            allowed_descendant_ids=(
                tuple(sorted(source.allowed_descendant_ids)) if source is not None else ()
            ),
            sync_cursor=source.sync_cursor if source is not None else None,
            status=source.status if source is not None else None,
            connection_identity=source.connection_identity if source is not None else None,
        )
        credential = await self._read_connector_credential(
            db_session, principal.organization_id
        )

        # Close the assessment transaction before any credential or Drive I/O.
        await db_session.rollback()
        try:
            refresh_token = await self._connector_service.decrypt_refresh_token(
                credential.encrypted_secret
            )
            connection = await self._drive_gateway_factory.create(
                refresh_token=refresh_token
            )
            root = await connection.gateway.get(root_folder_id)
        except DriveFileUnavailable as error:
            raise self._root_unavailable_error(error.reason.value) from error
        except Exception as error:
            self._raise_drive_reauthorization(
                error,
                credential.connector_id,
                credential.connector_secret_id,
            )
            raise
        if (
            root is None
            or root.removed
            or root.trashed
            or root.mime_type != DRIVE_FOLDER_MIME_TYPE
        ):
            raise self._root_unavailable_error("NOT_FOUND_OR_NO_ACCESS")
        try:
            descendant_ids = (
                await connection.gateway.resolve_descendant_folder_ids(root_folder_id)
                if include_descendants
                else set()
            )
        except Exception as error:
            self._raise_drive_reauthorization(
                error,
                credential.connector_id,
                credential.connector_secret_id,
            )
            raise

        # Apply only after revalidating ownership, the source generation, and
        # the exact authorization generation used for the Drive requests.
        await self._require_configuration_authorization(db_session, principal)
        connector = await db_session.scalar(
            select(Connector)
            .where(Connector.id == credential.connector_id)
            .with_for_update()
        )
        if (
            connector is None
            or connector.organization_id != principal.organization_id
            or connector.kind is not ConnectorKind.DRIVE
            or connector.status is not ConnectorStatus.ACTIVE
            or connector.secret_id != credential.connector_secret_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Google Drive authorization changed during scope validation",
            )
        knowledge_base = await db_session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.organization_id == principal.organization_id)
            .with_for_update()
        )
        source = await db_session.scalar(
            select(DriveSource)
            .where(DriveSource.organization_id == principal.organization_id)
            .with_for_update()
        )
        if not self._configuration_matches_snapshot(
            source, knowledge_base, source_snapshot
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Drive source changed during scope validation",
            )
        if knowledge_base is None:
            knowledge_base = KnowledgeBase(organization_id=principal.organization_id)
            db_session.add(knowledge_base)
            await db_session.flush()
        if source is None:
            source = DriveSource(
                organization_id=principal.organization_id,
                knowledge_base_id=knowledge_base.id,
                root_folder_id=root_folder_id,
                include_descendants=include_descendants,
                allowed_descendant_ids=sorted(descendant_ids),
                status=DriveSourceStatus.ACTIVE,
                connection_identity=connection.connection_identity,
            )
            db_session.add(source)
        else:
            source.root_folder_id = root_folder_id
            source.include_descendants = include_descendants
            source.allowed_descendant_ids = sorted(descendant_ids)
            # Reconfiguration reactivates only after the proposed root was
            # explicitly read and validated above.
            source.status = DriveSourceStatus.ACTIVE
            source.connection_identity = connection.connection_identity
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
                "connector_id": str(credential.connector_id),
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
                        "after": str(credential.connector_id),
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
        source_id = source.id
        organization_id = source.organization_id
        current_source = await db_session.scalar(
            select(DriveSource).where(
                DriveSource.id == source_id,
                DriveSource.organization_id == organization_id,
            )
        )
        if current_source is None or not self.is_file_authorized(current_source, file):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
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
        secret = await db_session.get(ConnectorSecret, connector.secret_id)
        if secret is None or secret.organization_id != organization_id:
            raise LookupError("connector secret is unavailable")
        snapshot = _DriveDownloadSnapshot(
            source_id=current_source.id,
            organization_id=current_source.organization_id,
            root_folder_id=current_source.root_folder_id,
            include_descendants=current_source.include_descendants,
            allowed_descendant_ids=tuple(sorted(current_source.allowed_descendant_ids)),
            status=current_source.status,
            connection_identity=current_source.connection_identity,
            connector_id=connector.id,
            connector_secret_id=connector.secret_id,
            encrypted_secret=EncryptedSecret(
                ciphertext=secret.ciphertext,
                encrypted_data_key=secret.encrypted_data_key,
                nonce=secret.nonce,
                algorithm=secret.algorithm,
                key_version=secret.key_version,
            ),
        )

        # End the read transaction before decryption, OAuth refresh, or Drive I/O.
        await db_session.rollback()
        refresh_token = await self._connector_service.decrypt_refresh_token(
            snapshot.encrypted_secret
        )
        connection = await self._drive_gateway_factory.create(
            refresh_token=refresh_token
        )
        if connection.connection_identity != snapshot.connection_identity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Google Drive authorization changed during download",
            )
        content = await connection.gateway.download(file.id)

        # Revalidate the authorization generation and scope after external I/O,
        # then close this read transaction before parsing begins.
        revalidated_source = await db_session.get(DriveSource, snapshot.source_id)
        revalidated_connector = await db_session.get(Connector, snapshot.connector_id)
        if (
            revalidated_source is None
            or revalidated_source.organization_id != snapshot.organization_id
            or revalidated_source.status is not snapshot.status
            or revalidated_source.root_folder_id != snapshot.root_folder_id
            or revalidated_source.include_descendants is not snapshot.include_descendants
            or tuple(sorted(revalidated_source.allowed_descendant_ids))
            != snapshot.allowed_descendant_ids
            or not self.is_file_authorized(revalidated_source, file)
            or revalidated_connector is None
            or revalidated_connector.organization_id != snapshot.organization_id
            or revalidated_connector.kind is not ConnectorKind.DRIVE
            or revalidated_connector.status is not ConnectorStatus.ACTIVE
            or revalidated_connector.secret_id != snapshot.connector_secret_id
        ):
            await db_session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Google Drive authorization changed during download",
            )
        await db_session.rollback()
        return content

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

    async def _read_connector_credential(
        self, db_session: AsyncSession, organization_id: UUID
    ) -> _ConnectorCredentialSnapshot:
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
        secret = await db_session.get(ConnectorSecret, connector.secret_id)
        if secret is None or secret.organization_id != connector.organization_id:
            raise LookupError("connector secret is unavailable")
        return _ConnectorCredentialSnapshot(
            connector_id=connector.id,
            connector_secret_id=connector.secret_id,
            encrypted_secret=EncryptedSecret(
                ciphertext=secret.ciphertext,
                encrypted_data_key=secret.encrypted_data_key,
                nonce=secret.nonce,
                algorithm=secret.algorithm,
                key_version=secret.key_version,
            ),
        )

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
        if is_drive_authorization_error(error):
            raise DriveReauthorizationRequired(
                connector_id=connector_id,
                secret_id=secret_id,
            ) from error

    @staticmethod
    def _configuration_matches_snapshot(
        source: DriveSource | None,
        knowledge_base: KnowledgeBase | None,
        snapshot: _SourceConfigurationSnapshot,
    ) -> bool:
        if snapshot.source_id is None:
            return source is None and (
                snapshot.knowledge_base_id is None
                or (
                    knowledge_base is not None
                    and knowledge_base.id == snapshot.knowledge_base_id
                )
            )
        return bool(
            source is not None
            and knowledge_base is not None
            and source.id == snapshot.source_id
            and knowledge_base.id == snapshot.knowledge_base_id
            and source.knowledge_base_id == knowledge_base.id
            and source.root_folder_id == snapshot.root_folder_id
            and source.include_descendants is snapshot.include_descendants
            and tuple(sorted(source.allowed_descendant_ids))
            == snapshot.allowed_descendant_ids
            and source.sync_cursor == snapshot.sync_cursor
            and source.status is snapshot.status
            and source.connection_identity == snapshot.connection_identity
        )

    @staticmethod
    def _root_unavailable_error(reason: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GOOGLE_DRIVE_ROOT_UNAVAILABLE",
                "message": (
                    "The selected Google Drive root folder is unavailable. "
                    "Restore access or choose another folder."
                ),
                "reason": reason,
            },
        )

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
