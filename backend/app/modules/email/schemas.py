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


class EmailDraftResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    work_item_id: UUID
    state: EmailState
    body: str | None
    citations: list[EmailCitation]
    provenance: EmailDraftProvenance | None
    error_code: str | None = None
