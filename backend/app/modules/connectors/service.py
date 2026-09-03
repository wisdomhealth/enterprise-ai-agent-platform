from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import Action, ResourceRef, ResourceState
from app.modules.connectors.encryption import (
    EncryptedSecret,
    EnvelopeCipher,
    FileKeyWrapper,
    envelope_cipher_from_settings,
)
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.identity.dependencies import Principal
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
        cipher = envelope_cipher_from_settings(settings)
        return cls(cipher) if cipher is not None else None

    @staticmethod
    def configuration_resource_id(organization_id: UUID, kind: ConnectorKind) -> UUID:
        return uuid5(NAMESPACE_URL, f"connector-configuration:{organization_id}:{kind.value}")

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

    async def create_or_reauthorize(
        self,
        db_session: AsyncSession,
        *,
        principal: Principal,
        kind: ConnectorKind,
        refresh_token: str,
    ) -> Connector:
        connector = await self.require_authorization_start(
            db_session, principal=principal, kind=kind
        )
        connector = await self._create_or_reauthorize(
            db_session,
            organization_id=principal.organization_id,
            kind=kind,
            refresh_token=refresh_token,
            connector=connector,
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

    async def require_authorization_start(
        self,
        db_session: AsyncSession,
        *,
        principal: Principal,
        kind: ConnectorKind,
    ) -> Connector | None:
        connector = await db_session.scalar(
            select(Connector).where(
                Connector.organization_id == principal.organization_id,
                Connector.kind == kind,
            )
        )
        if connector is None:
            connector_id = self.configuration_resource_id(principal.organization_id, kind)
            await self._require_action(
                db_session,
                principal,
                "connector.create",
                ResourceRef(
                    organization_id=principal.organization_id,
                    resource_type="connector",
                    resource_id=connector_id,
                    state=ResourceState.ACTIVE,
                ),
            )
        else:
            if connector.status is ConnectorStatus.ACTIVE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="connector is active; revoke it before reauthorization",
                )
            await self._require_action(
                db_session,
                principal,
                "connector.reauthorize",
                self._resource_ref(connector),
            )
        return connector

    async def revoke(
        self, db_session: AsyncSession, *, principal: Principal, connector_id: UUID
    ) -> Connector:
        connector = await db_session.scalar(
            select(Connector).where(
                Connector.id == connector_id, Connector.organization_id == principal.organization_id
            )
        )
        if connector is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if connector.status is not ConnectorStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="connector is not active",
            )
        await self._require_action(
            db_session,
            principal,
            "connector.revoke",
            self._resource_ref(connector),
        )
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
        connector: Connector | None,
    ) -> Connector:
        secret = await self.store_refresh_token(
            db_session, organization_id=organization_id, refresh_token=refresh_token
        )
        if connector is None:
            connector = Connector(
                id=self.configuration_resource_id(organization_id, kind),
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

    async def _require_action(
        self,
        db_session: AsyncSession,
        principal: Principal,
        action: Action,
        resource: ResourceRef,
    ) -> None:
        try:
            await AuthorizationService(db_session).require(principal, action, resource)
        except AuthorizationDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc

    @staticmethod
    def _resource_ref(connector: Connector) -> ResourceRef:
        states = {
            ConnectorStatus.ACTIVE: ResourceState.ACTIVE,
            ConnectorStatus.REAUTH_REQUIRED: ResourceState.REAUTH_REQUIRED,
            ConnectorStatus.ERROR: ResourceState.ERROR,
        }
        return ResourceRef(
            organization_id=connector.organization_id,
            resource_type="connector",
            resource_id=connector.id,
            state=states[connector.status],
        )
