from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.audit.service import AuditService
from app.modules.connectors.encryption import (
    EncryptedSecret,
    EnvelopeCipher,
    FileKeyWrapper,
    GoogleCloudKmsKeyWrapper,
)
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.outbox.service import OutboxService


class ConnectorService:
    def __init__(
        self,
        cipher: EnvelopeCipher,
        *,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._cipher = cipher
        self._audit_service = audit_service or AuditService()
        self._outbox_service = outbox_service or OutboxService()

    @classmethod
    def for_file_key(
        cls, key_path: Path, *, app_env: str, self_hosted_file_key_allowed: bool = False
    ) -> "ConnectorService":
        return cls(
            EnvelopeCipher(
                FileKeyWrapper(
                    key_path,
                    app_env=app_env,
                    self_hosted_file_key_allowed=self_hosted_file_key_allowed,
                )
            )
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "ConnectorService | None":
        if settings.google_kms_key_name is not None:
            return cls(EnvelopeCipher(GoogleCloudKmsKeyWrapper(settings.google_kms_key_name)))
        if settings.connector_file_key_path is None:
            return None
        return cls.for_file_key(
            settings.connector_file_key_path,
            app_env=settings.app_env,
            self_hosted_file_key_allowed=settings.self_hosted_file_key_allowed,
        )

    def require_admin(self, principal: Principal) -> None:
        if principal.role is not UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    async def store_refresh_token(
        self, db_session: AsyncSession, *, organization_id: UUID, refresh_token: str
    ) -> ConnectorSecret:
        encrypted = await self._cipher.encrypt(refresh_token)
        secret = ConnectorSecret(
            organization_id=organization_id,
            ciphertext=encrypted.ciphertext,
            encrypted_data_key=encrypted.encrypted_data_key,
            nonce=encrypted.nonce,
            algorithm=encrypted.algorithm,
            key_version=encrypted.key_version,
        )
        db_session.add(secret)
        await db_session.flush()
        return secret

    async def load_refresh_token(self, db_session: AsyncSession, connector: Connector) -> str:
        secret = await db_session.get(ConnectorSecret, connector.secret_id)
        if secret is None or secret.organization_id != connector.organization_id:
            raise LookupError("connector secret is unavailable")
        return await self._cipher.decrypt(
            EncryptedSecret(
                ciphertext=secret.ciphertext,
                encrypted_data_key=secret.encrypted_data_key,
                nonce=secret.nonce,
                algorithm=secret.algorithm,
                key_version=secret.key_version,
            )
        )

    async def create_drive_connector(
        self, db_session: AsyncSession, *, organization_id: UUID, refresh_token: str
    ) -> Connector:
        return await self._create_or_reauthorize(
            db_session,
            organization_id=organization_id,
            kind=ConnectorKind.DRIVE,
            refresh_token=refresh_token,
        )

    async def create_or_reauthorize(
        self,
        db_session: AsyncSession,
        *,
        principal: Principal,
        kind: ConnectorKind,
        refresh_token: str,
    ) -> Connector:
        self.require_admin(principal)
        connector = await self._create_or_reauthorize(
            db_session,
            organization_id=principal.organization_id,
            kind=kind,
            refresh_token=refresh_token,
        )
        await self._audit_service.record(
            db_session,
            principal,
            action="connector.authorize",
            object_type="connector",
            object_id=connector.id,
            outcome="SUCCESS",
            details={"kind": kind.value},
            safe_detail_keys=("kind",),
        )
        await self._outbox_service.add(
            db_session,
            "connector.authorized",
            "connector",
            connector.id,
            {"organization_id": str(principal.organization_id), "kind": kind.value},
        )
        return connector

    async def revoke(
        self, db_session: AsyncSession, *, principal: Principal, connector_id: UUID
    ) -> Connector:
        self.require_admin(principal)
        connector = await db_session.scalar(
            select(Connector).where(
                Connector.id == connector_id, Connector.organization_id == principal.organization_id
            )
        )
        if connector is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        connector.status = ConnectorStatus.REAUTH_REQUIRED
        await db_session.flush()
        await self._audit_service.record(
            db_session,
            principal,
            action="connector.revoke",
            object_type="connector",
            object_id=connector.id,
            outcome="SUCCESS",
        )
        await self._outbox_service.add(
            db_session,
            "connector.revoked",
            "connector",
            connector.id,
            {"organization_id": str(principal.organization_id)},
        )
        return connector

    async def _create_or_reauthorize(
        self,
        db_session: AsyncSession,
        *,
        organization_id: UUID,
        kind: ConnectorKind,
        refresh_token: str,
    ) -> Connector:
        secret = await self.store_refresh_token(
            db_session, organization_id=organization_id, refresh_token=refresh_token
        )
        connector = await db_session.scalar(
            select(Connector).where(
                Connector.organization_id == organization_id, Connector.kind == kind
            )
        )
        if connector is None:
            connector = Connector(
                organization_id=organization_id,
                kind=kind,
                status=ConnectorStatus.ACTIVE,
                secret_id=secret.id,
            )
            db_session.add(connector)
        else:
            connector.secret_id = secret.id
            connector.status = ConnectorStatus.ACTIVE
        await db_session.flush()
        return connector
