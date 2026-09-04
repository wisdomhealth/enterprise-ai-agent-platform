from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.modules.email.models import EmailCategory, EmailPriority, EmailState


class EmailClassification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: EmailCategory
    priority: EmailPriority
    reply_required: StrictBool

    @model_validator(mode="after")
    def require_consistent_reply_flag(self) -> "EmailClassification":
        expected = self.category in {EmailCategory.ACTION_REQUIRED, EmailCategory.UNKNOWN}
        if self.reply_required is not expected:
            raise ValueError("reply_required does not match category")
        return self


class EmailCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: UUID
    knowledge_base_id: UUID
    chunk_id: UUID
    document_version_id: UUID
    title: str
    section: str | None
    page_number: int | None
    internal_drive_link: str | None = None


class EmailDraftProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    prompt_version: str
    retrieval_chunk_ids: list[UUID]
    retrieval_document_version_ids: list[UUID]
    retrieval_latency_ms: int = Field(ge=0)
    model_latency_ms: int = Field(ge=0)
    end_to_end_latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)
    retrieval_principal_id: UUID
    retrieval_actor_type: str = "STAFF"


class EmailDraftResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    work_item_id: UUID
    state: EmailState
    body: str | None
    citations: list[EmailCitation]
    provenance: EmailDraftProvenance | None
    error_code: str | None = None


class StaffEmailQueueItem(BaseModel):
    """Safe staff-facing projection of an email work item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    state: EmailState
    version: int = Field(ge=1)
    sender: str
    subject: str
    received_at: datetime
    category: EmailCategory | None
    priority: EmailPriority | None


class StaffEmailCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    section: str | None
    page_number: int | None
    chunk_id: UUID
    document_version_id: UUID
    internal_drive_link: str | None


class StaffEmailApproval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approved_at: datetime
    invalidated_at: datetime | None


class StaffEmailDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    version: int = Field(ge=1)
    body: str
    to: list[str]
    cc: list[str]
    subject: str
    thread_id: str
    reviewer_instruction: str | None
    model: str
    prompt_version: str
    created_at: datetime
    citations: list[StaffEmailCitation]
    approval: StaffEmailApproval | None


class StaffEmailAuditTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    from_state: EmailState
    to_state: EmailState
    action: str
    reason_code: str | None
    actor_type: Literal["SYSTEM", "STAFF"]
    created_at: datetime


class StaffEmailDeliveryAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    attempt_number: int = Field(ge=1)
    outcome: Literal["IN_PROGRESS", "SENT", "DEFINITIVE_FAILURE", "UNKNOWN"]
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None


class StaffEmailDelivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    state: EmailState
    version: int = Field(ge=1)
    deterministic_message_id: str
    last_error_code: str | None
    attempts: list[StaffEmailDeliveryAttempt]


class StaffEmailDetail(StaffEmailQueueItem):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recipients: list[str]
    body: str
    reply_required: bool | None
    classification_rationale: str
    current_draft_id: UUID | None
    drafts: list[StaffEmailDraft]
    audit_transitions: list[StaffEmailAuditTransition]
    delivery: StaffEmailDelivery | None
