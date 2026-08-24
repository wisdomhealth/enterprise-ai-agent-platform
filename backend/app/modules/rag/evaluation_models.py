from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RAGEvaluationRun(Base):
    __tablename__ = "rag_evaluation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_rag_evaluation_runs_status"),
        CheckConstraint("length(dataset_digest) = 64", name="ck_rag_evaluation_runs_digest"),
        Index("ix_rag_evaluation_runs_organization_completed", "organization_id", "completed_at"),
    )
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(200), nullable=False)
    dataset_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    document_version_set: Mapped[str] = mapped_column(String(500), nullable=False)
    chunking_version: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    retrieval_config: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(200), nullable=False)
    llm_model: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    hard_gates: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RAGEvaluationCase(Base):
    __tablename__ = "rag_evaluation_cases"
    __table_args__ = (
        CheckConstraint(
            "length(question_digest) = 64", name="ck_rag_evaluation_cases_question_digest"
        ),
        Index("ix_rag_evaluation_cases_run", "run_id"),
    )
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("rag_evaluation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    case_id: Mapped[str] = mapped_column(String(200), nullable=False)
    question_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    answer_refused: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retrieved_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PostgreSQLUUID(as_uuid=True)), nullable=False
    )
    retrieved_document_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PostgreSQLUUID(as_uuid=True)), nullable=False
    )
    retrieved_document_version_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PostgreSQLUUID(as_uuid=True)), nullable=False
    )
    citation_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PostgreSQLUUID(as_uuid=True)), nullable=False
    )
    citation_document_version_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PostgreSQLUUID(as_uuid=True)), nullable=False
    )
