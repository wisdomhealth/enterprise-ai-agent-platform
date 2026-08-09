from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.identity.models import UserRole, UserStatus


class OrganizationCreate(BaseModel):
    name: str


class OrganizationRead(OrganizationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class StaffUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    oidc_subject: str
    email: str
    role: UserRole
    status: UserStatus
    version: int
