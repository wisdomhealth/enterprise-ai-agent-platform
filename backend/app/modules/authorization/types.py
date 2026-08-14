from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

type Action = Literal[
    "knowledge.read",
    "knowledge.write",
    "knowledge.review",
    "knowledge.publish",
]
type ResourceType = Literal["knowledge"]


class ResourceState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    organization_id: UUID
    resource_type: ResourceType
    resource_id: UUID
    state: ResourceState
    is_public: bool = False
