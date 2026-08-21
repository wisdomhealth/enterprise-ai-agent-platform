from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.dependencies import Principal, get_db_session, require_staff_csrf
from app.modules.knowledge.drive_gateway import DriveGateway
from app.modules.knowledge.schemas import DriveSourceConfigure, DriveSourceRead
from app.modules.knowledge.service import KnowledgeSourceService

router = APIRouter(prefix="/api/v1/admin/knowledge-sources", tags=["knowledge-sources"])


def _knowledge_source_service(request: Request) -> KnowledgeSourceService:
    gateway = getattr(request.app.state, "drive_gateway", None)
    if not isinstance(gateway, DriveGateway):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Drive read-only gateway is not configured",
        )
    return KnowledgeSourceService(gateway)


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
        connection_identity=payload.connection_identity,
    )
    await db_session.commit()
    return DriveSourceRead.model_validate(source)
