from datetime import datetime
from enum import StrEnum
from secrets import token_urlsafe
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DriveSourceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class DocumentVersionState(StrEnum):
    PROCESSING = "PROCESSING"
    RETRIEVABLE = "RETRIEVABLE"
    FAILED = "FAILED"
    REVOKED = "REVOKED"
    DELETED = "DELETED"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_knowledge_bases_organization"),)

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    default_language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en", server_default=sql_text("'en'")
    )
    public_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        default=lambda: token_urlsafe(24),
        server_default=sql_text("replace(gen_random_uuid()::text, '-', '')"),
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
        Index("ix_drive_sources_organization", "organization_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
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
        Boolean, nullable=False, default=True, server_default=sql_text("true")
    )
    allowed_descendant_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    sync_cursor: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[DriveSourceStatus] = mapped_column(
        Enum(DriveSourceStatus, name="drive_source_status"),
        nullable=False,
        default=DriveSourceStatus.ACTIVE,
        server_default=sql_text("'ACTIVE'::drive_source_status"),
    )
    connection_identity: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
        Index("ix_documents_knowledge_base", "knowledge_base_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
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
    source_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("drive_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    current_version: Mapped["DocumentVersion | None"] = relationship(
        "DocumentVersion",
        foreign_keys=[current_version_id],
        post_update=True,
    )
    versions: Mapped[list["DocumentVersion"]] = relationship(
        "DocumentVersion",
        back_populates="document",
        foreign_keys="DocumentVersion.document_id",
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "content_sha256", name="uq_document_versions_content"),
        Index("ix_document_versions_document_state", "document_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    document_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[DocumentVersionState] = mapped_column(
        Enum(DocumentVersionState, name="document_version_state"),
        nullable=False,
        default=DocumentVersionState.PROCESSING,
        server_default=sql_text("'PROCESSING'::document_version_state"),
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    document: Mapped[Document] = relationship(
        "Document", back_populates="versions", foreign_keys=[document_id]
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "ordinal", name="uq_document_chunks_version_ordinal"
        ),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_document_chunks_search_vector_gin",
            "search_vector",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    document_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    search_vector: Mapped[object] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
