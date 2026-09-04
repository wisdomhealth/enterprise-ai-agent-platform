from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.chat.models import ChatActor, ChatMessageStatus, ConversationState


class PublicChatSessionCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    public_key: str = Field(min_length=16, max_length=64)
    customer_name: str | None = Field(default=None, max_length=200)
    customer_email: str | None = Field(default=None, max_length=320)

    @field_validator("customer_email")
    @classmethod
    def validate_optional_email(cls, value: str | None) -> str | None:
        if value is not None and "@" not in value:
            raise ValueError("email must be valid")
        return value


class PublicChatMessageCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    body: str = Field(min_length=1, max_length=4_000)


class PublicChatCredentialRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str
    expires_at: datetime


class PublicChatMessageRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    actor: ChatActor
    body: str
    status: ChatMessageStatus
    created_at: datetime


class PublicChatSessionRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    state: ConversationState
    version: int
    customer_name: str | None
    customer_email: str | None
    created_at: datetime
    messages: list[PublicChatMessageRead]


class PublicChatSessionCreated(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session: PublicChatSessionRead
    credential: PublicChatCredentialRead
