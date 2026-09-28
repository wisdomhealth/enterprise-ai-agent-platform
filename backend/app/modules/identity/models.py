from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    REVIEWER = "REVIEWER"
    MEMBER = "MEMBER"


class UserStatus(StrEnum):
    INVITED = "INVITED"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class Organization(Base):
    __tablename__ = "organizations"

    # 组织的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 组织显示名称。
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class StaffUser(Base):
    __tablename__ = "staff_users"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "oidc_subject",
            name="uq_staff_users_organization_oidc_subject",
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_staff_users_organization_id_id",
        ),
    )

    # 员工用户的唯一标识。
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
    # OIDC 身份提供方中的用户主体标识。
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 员工登录邮箱。
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # 员工在组织内的角色。
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)
    # 记录当前业务状态。
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), nullable=False
    )
    # 并发控制版本号，用于检测和阻止并发覆盖。
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )


class StaffSession(Base):
    __tablename__ = "staff_sessions"
    __table_args__ = (Index("ix_staff_sessions_user_id", "user_id"),)

    # 员工会话的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 关联员工用户的唯一标识。
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CSRF 令牌的安全哈希值。
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 凭证或租约的过期时间。
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 凭证被撤销的时间；为空表示尚未撤销。
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
