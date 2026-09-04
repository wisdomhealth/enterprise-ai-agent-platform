"""Persist append-only RAG evaluation run and case provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_rag_evaluation_runs"
down_revision: str | None = "0010_vector_and_fulltext_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rag_evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_version", sa.String(200), nullable=False),
        sa.Column("dataset_kind", sa.String(32), nullable=False),
        sa.Column("dataset_digest", sa.String(64), nullable=False),
        sa.Column("document_version_set", sa.String(500), nullable=False),
        sa.Column("chunking_version", sa.String(200), nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("retrieval_config", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_version", sa.String(200), nullable=False),
        sa.Column("llm_model", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("hard_gates", postgresql.JSONB(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_rag_evaluation_runs_status"),
        sa.CheckConstraint("length(dataset_digest) = 64", name="ck_rag_evaluation_runs_digest"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rag_evaluation_runs_organization_completed", "rag_evaluation_runs", ["organization_id", "completed_at"])
    op.create_table(
        "rag_evaluation_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", sa.String(200), nullable=False),
        sa.Column("question_digest", sa.String(64), nullable=False),
        sa.Column("answer_refused", sa.Boolean(), nullable=False),
        sa.Column("retrieved_chunk_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("retrieved_document_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("retrieved_document_version_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("citation_chunk_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("citation_document_version_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.CheckConstraint("length(question_digest) = 64", name="ck_rag_evaluation_cases_question_digest"),
        sa.ForeignKeyConstraint(["run_id"], ["rag_evaluation_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rag_evaluation_cases_run", "rag_evaluation_cases", ["run_id"])
    op.execute("GRANT SELECT, INSERT ON rag_evaluation_runs, rag_evaluation_cases TO platform_app")


def downgrade() -> None:
    op.drop_index("ix_rag_evaluation_cases_run", table_name="rag_evaluation_cases")
    op.drop_table("rag_evaluation_cases")
    op.drop_index("ix_rag_evaluation_runs_organization_completed", table_name="rag_evaluation_runs")
    op.drop_table("rag_evaluation_runs")
