from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_csrf
from app.modules.knowledge.schemas import DriveSourceConfigure, DriveSourceRead
from app.modules.knowledge.service import KnowledgeSourceService

router = APIRouter(prefix="/api/v1/admin/knowledge-sources", tags=["knowledge-sources"])


def _knowledge_source_service(request: Request) -> KnowledgeSourceService:
    connector_service = getattr(request.app.state, "connector_service", None)
    gateway_factory = getattr(request.app.state, "drive_gateway_factory", None)
    if not isinstance(connector_service, ConnectorService) or gateway_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Drive read-only connector is not configured",
        )
    return KnowledgeSourceService(connector_service, gateway_factory)


@router.put("/drive", response_model=DriveSourceRead)
async def configure_drive_source(
    payload: DriveSourceConfigure,
    db_session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_staff_csrf),
    service: KnowledgeSourceService = Depends(_knowledge_source_service),
) -> DriveSourceRead:
    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id=payload.root_folder_id,
        include_descendants=payload.include_descendants,
    )
    await db_session.commit()
    return DriveSourceRead.model_validate(source)
