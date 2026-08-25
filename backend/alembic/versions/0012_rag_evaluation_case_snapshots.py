"""Make RAG evaluation case records complete immutable snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_rag_evaluation_case_snapshots"
down_revision: str | None = "0011_rag_evaluation_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rag_evaluation_cases",
        sa.Column("snapshot", postgresql.JSONB(), nullable=True),
    )
    op.execute(
        """
        UPDATE rag_evaluation_cases
        SET snapshot = jsonb_build_object(
            'legacy', true, 'case_id', case_id, 'question_digest', question_digest,
            'answer_refused', answer_refused, 'retrieved_chunk_ids', retrieved_chunk_ids,
            'retrieved_document_ids', retrieved_document_ids,
            'retrieved_document_version_ids', retrieved_document_version_ids,
            'citation_chunk_ids', citation_chunk_ids,
            'citation_document_version_ids', citation_document_version_ids
        )
        WHERE snapshot IS NULL
        """
    )
    op.alter_column("rag_evaluation_cases", "snapshot", nullable=False)
    op.execute("REVOKE UPDATE, DELETE ON rag_evaluation_runs, rag_evaluation_cases FROM platform_app")


def downgrade() -> None:
    op.drop_column("rag_evaluation_cases", "snapshot")
