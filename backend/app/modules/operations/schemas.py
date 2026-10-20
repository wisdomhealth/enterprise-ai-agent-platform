from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    disabled_reason: str | None = None


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
    RETRY_EMAIL_CLASSIFICATION = "RETRY_EMAIL_CLASSIFICATION"
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


class JobRetryRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(ge=1)


class ConnectorReauthorizationRead(BaseModel):
    connector_id: UUID
    authorization_url: str
    requested_scopes: list[str]


class ConnectorAuthorizationActionRead(BaseModel):
    action: Literal["connector.create", "connector.reauthorize", "connector.revoke"]
    granted: bool


class ConnectorAuthorizationRead(BaseModel):
    staff_user_id: UUID
    kind: ConnectorKind
    resource_id: UUID
    authorize_endpoint: str
    actions: list[ConnectorAuthorizationActionRead]


class ConnectorGrantReplace(BaseModel):
    staff_user_id: UUID
    actions: list[Literal["connector.create", "connector.reauthorize", "connector.revoke"]] = Field(
        default_factory=list
    )


class ApplicationAccessRead(BaseModel):
    staff_user_id: UUID
    capability: Literal["email", "support"]
    resource_id: UUID
    grant_present: bool
    role_status_eligible: bool
    reviewable_knowledge_base_ids: list[UUID]
    effective_allowed: bool


class ApplicationGrantReplace(BaseModel):
    granted: bool
    expected_user_version: int = Field(ge=1)


class GmailSyncStateRead(BaseModel):
    connector_id: UUID
    mailbox_address: str | None
    auto_sync_enabled: bool
    polling_enabled: bool
    push_enabled: bool
    watch_status: str
    sync_status: str
    processed_history_id: str | None
    pending_history_id: str | None
    watch_expiration_at: datetime | None
    recovery_batch_id: UUID | None
    recovery_window_started_at: datetime | None
    recovery_processed_count: int
    recovery_message_limit: int
    recovery_has_more: bool
    last_error_code: str | None
    last_success_at: datetime | None
    active_job_id: UUID | None
    active_job_state: str | None
    active_job_trigger: str | None
    version: int


class GmailRecoveryContinueRequest(BaseModel):
    expected_version: int = Field(ge=1)
    additional_messages: int = Field(ge=1, le=10_000)
    reason: str = Field(min_length=1, max_length=1000)


class GmailRecoveryAcceptRequest(BaseModel):
    expected_version: int = Field(ge=1)
    confirmed: bool
    reason: str = Field(min_length=1, max_length=1000)


class GmailManualSyncRequest(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
