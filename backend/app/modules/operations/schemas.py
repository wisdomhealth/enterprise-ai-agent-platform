from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.connectors.models import ConnectorKind, ConnectorStatus


class ConnectorStatusRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: ConnectorKind
    status: ConnectorStatus
    updated_at: datetime
    requested_scopes: list[str]


class KnowledgeSourceStatusRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: UUID
    status: str
    root_folder_id: str
    include_descendants: bool
    descendant_count: int
    cursor: str | None
    last_success_at: datetime | None
    backlog: int
    isolated_files: int
    retry_count: int
    recent_error_codes: list[str]


class JobSummaryRead(BaseModel):
    queue_depth: int
    failed: int


class SupportSummaryRead(BaseModel):
    backlog: int


class EmailSummaryRead(BaseModel):
    retry_wait: int
    delivery_unknown: int


class QualityStatusRead(BaseModel):
    completed_at: datetime
    status: str
    quality_score: float
    latency_ms: float
    estimated_cost: float


class OperationsSummaryRead(BaseModel):
    generated_at: datetime
    connectors: list[ConnectorStatusRead]
    knowledge_sources: list[KnowledgeSourceStatusRead]
    jobs: JobSummaryRead
    support: SupportSummaryRead
    email: EmailSummaryRead
    rag_quality: QualityStatusRead | None
    email_quality: QualityStatusRead | None


class JobAction(StrEnum):
    RETRY_DRIVE_SYNC = "RETRY_DRIVE_SYNC"
    RETRY_EMAIL_DELIVERY = "RETRY_EMAIL_DELIVERY"
    RECONCILE_GMAIL = "RECONCILE_GMAIL"
    NONE = "NONE"


class FailedJobRead(BaseModel):
    job_id: UUID
    kind: str
    state: str
    attempts: int
    error_code: str | None
    updated_at: datetime
    action: JobAction
    action_resource_id: UUID | None


class JobRetryRead(BaseModel):
    job_id: UUID
    state: str
    version: int


class ConnectorReauthorizationRead(BaseModel):
    connector_id: UUID
    authorization_url: str
    requested_scopes: list[str]
