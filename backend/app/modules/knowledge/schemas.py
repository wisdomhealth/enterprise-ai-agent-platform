from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.knowledge.models import DriveSourceStatus


class DriveSourceConfigure(BaseModel):
    root_folder_id: str = Field(min_length=1, max_length=512)
    include_descendants: bool = True
    connection_identity: str = Field(min_length=1, max_length=320)


class DriveSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    knowledge_base_id: UUID
    root_folder_id: str
    include_descendants: bool
    sync_cursor: str | None
    status: DriveSourceStatus
    connection_identity: str
