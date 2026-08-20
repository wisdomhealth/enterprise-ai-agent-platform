from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

type Action = Literal[
    "connector.create",
    "connector.reauthorize",
    "connector.revoke",
    "knowledge.read",
    "knowledge.write",
    "knowledge.review",
    "knowledge.publish",
]
type ResourceType = Literal["connector", "knowledge"]


class ResourceState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DISABLED = "DISABLED"
    ERROR = "ERROR"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    organization_id: UUID
    resource_type: ResourceType
    resource_id: UUID
    state: ResourceState
    is_public: bool = False
