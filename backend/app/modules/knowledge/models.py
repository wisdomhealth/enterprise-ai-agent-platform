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

    # 知识库的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 知识库默认语言代码。
    default_language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en", server_default=sql_text("'en'")
    )
    # 知识库对外引用使用的随机公开键。
    public_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        default=lambda: token_urlsafe(24),
        server_default=sql_text("replace(gen_random_uuid()::text, '-', '')"),
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DriveSource(Base):
    __tablename__ = "drive_sources"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", name="uq_drive_sources_knowledge_base"),
        Index("ix_drive_sources_organization", "organization_id"),
    )

    # Google Drive 数据源的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所属知识库的唯一标识。
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Google Drive 同步根目录的外部标识。
    root_folder_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # 标记同步范围是否包含根目录的所有后代目录。
    include_descendants: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sql_text("true")
    )
    # 经过授权、允许同步的后代目录标识集合。
    allowed_descendant_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    # Google Drive Changes API 的持久化增量同步游标。
    sync_cursor: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 记录当前业务状态。
    status: Mapped[DriveSourceStatus] = mapped_column(
        Enum(DriveSourceStatus, name="drive_source_status"),
        nullable=False,
        default=DriveSourceStatus.ACTIVE,
        server_default=sql_text("'ACTIVE'::drive_source_status"),
    )
    # 连接 Google Drive 时使用的账号身份。
    connection_identity: Mapped[str] = mapped_column(String(320), nullable=False)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
        Index("ix_documents_knowledge_base", "knowledge_base_id"),
    )

    # 知识文档的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所属知识库的唯一标识。
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 文档所属外部数据源的唯一标识。
    source_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("drive_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 文档在外部数据源中的唯一标识。
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # 文档标题。
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 文档 MIME 类型，用于选择解析器。
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    # 当前可检索文档版本的唯一标识。
    current_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
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

    # 文档版本的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=sql_text("gen_random_uuid()"),
    )
    # 所属文档的唯一标识。
    document_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 记录当前状态机状态。
    state: Mapped[DocumentVersionState] = mapped_column(
        Enum(DocumentVersionState, name="document_version_state"),
        nullable=False,
        default=DocumentVersionState.PROCESSING,
        server_default=sql_text("'PROCESSING'::document_version_state"),
    )
    # 原始文档内容的 SHA-256 摘要，用于版本去重。
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 本次处理失败的标准错误码。
    error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
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

    # 文档分块的唯一标识。
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    # 所属文档版本的唯一标识。
    document_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 分块在文档版本中的稳定顺序编号。
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # 分块的可检索文本内容。
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 分块来源页码；无法确定时为空。
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 分块所属章节名称；无法确定时为空。
    section: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 分块文本的令牌数量。
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 分块的附加结构化元数据。
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    # 分块文本的向量表示，用于语义检索。
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    # 由数据库维护的全文检索向量。
    search_vector: Mapped[object] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
