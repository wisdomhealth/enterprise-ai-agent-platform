from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ResourceGrant(Base):
    __tablename__ = "resource_grants"
    __table_args__ = (
        CheckConstraint("cardinality(actions) > 0", name="ck_resource_grants_actions_nonempty"),
        UniqueConstraint(
            "organization_id",
            "subject_id",
            "resource_type",
            "resource_id",
            name="uq_resource_grants_subject_resource",
        ),
        Index(
            "ix_resource_grants_subject_lookup",
            "organization_id",
            "subject_id",
            "resource_type",
            "resource_id",
        ),
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
    subject_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    actions: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False)
