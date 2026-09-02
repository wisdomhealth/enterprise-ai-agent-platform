from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    oidc_subject: str | None
    email: str
    role: UserRole
    status: UserStatus
    version: int


class StaffInvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    role: UserRole


class StaffUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    role: UserRole | None = None
    status: UserStatus | None = None

    @model_validator(mode="after")
    def require_change(self) -> "StaffUserPatch":
        if self.role is None and self.status is None:
            raise ValueError("role or status is required")
        return self


class AdminUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: UserRole
    status: UserStatus
    version: int
