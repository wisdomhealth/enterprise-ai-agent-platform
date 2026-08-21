from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.knowledge.drive_gateway import DriveFile, DriveGateway
from app.modules.knowledge.models import DriveSource, DriveSourceStatus, KnowledgeBase
from app.modules.knowledge.scope import DriveScope


class KnowledgeSourceService:
    def __init__(self, drive_gateway: DriveGateway) -> None:
        self._drive_gateway = drive_gateway

    @staticmethod
    def configuration_resource_id(organization_id: UUID) -> UUID:
        return uuid5(NAMESPACE_URL, f"knowledge-source-configuration:{organization_id}")

    async def configure_drive_source(
        self,
        db_session: AsyncSession,
        *,
        principal: Principal,
        root_folder_id: str,
        connection_identity: str,
        include_descendants: bool = True,
    ) -> DriveSource:
        if principal.role is not UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        await self._require_configuration_authorization(db_session, principal)

        descendant_ids = (
            await self._drive_gateway.resolve_descendant_folder_ids(root_folder_id)
            if include_descendants
            else set()
        )
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
        if source is None:
            source = DriveSource(
                organization_id=principal.organization_id,
                knowledge_base_id=knowledge_base.id,
                root_folder_id=root_folder_id,
                include_descendants=include_descendants,
                allowed_descendant_ids=sorted(descendant_ids),
                status=DriveSourceStatus.ACTIVE,
                connection_identity=connection_identity,
            )
            db_session.add(source)
        else:
            source.root_folder_id = root_folder_id
            source.include_descendants = include_descendants
            source.allowed_descendant_ids = sorted(descendant_ids)
            source.status = DriveSourceStatus.ACTIVE
            source.connection_identity = connection_identity
        await db_session.flush()
        return source

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
