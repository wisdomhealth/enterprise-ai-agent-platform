from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
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
        ForeignKeyConstraint(
            ["organization_id", "subject_id"],
            ["staff_users.organization_id", "staff_users.id"],
            name="fk_resource_grants_organization_subject",
            ondelete="CASCADE",
        ),
        Index(
            "ix_resource_grants_subject_lookup",
            "organization_id",
            "subject_id",
            "resource_type",
            "resource_id",
        ),
    )

    # 资源授权的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 获得资源权限的主体唯一标识。
    subject_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    # 授权资源的类型。
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # 授权资源的唯一标识。
    resource_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    # 允许主体在资源上执行的操作集合。
    actions: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False)
