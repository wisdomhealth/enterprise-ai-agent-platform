from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DriveSourceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_knowledge_bases_organization"),)

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    default_language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en", server_default=text("'en'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DriveSource(Base):
    __tablename__ = "drive_sources"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", name="uq_drive_sources_knowledge_base"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    root_folder_id: Mapped[str] = mapped_column(String(512), nullable=False)
    include_descendants: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    allowed_descendant_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    sync_cursor: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[DriveSourceStatus] = mapped_column(
        Enum(DriveSourceStatus, name="drive_source_status"),
        nullable=False,
        default=DriveSourceStatus.ACTIVE,
        server_default=text("'ACTIVE'::drive_source_status"),
    )
    connection_identity: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
