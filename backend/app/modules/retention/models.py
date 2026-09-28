from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ErasureScope(StrEnum):
    CUSTOMER = "CUSTOMER"
    KNOWLEDGE_DOCUMENT = "KNOWLEDGE_DOCUMENT"


class ErasureStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class ErasureTargetType(StrEnum):
    CHAT_SESSION = "CHAT_SESSION"
    EMAIL_WORK_ITEM = "EMAIL_WORK_ITEM"
    KNOWLEDGE_DOCUMENT = "KNOWLEDGE_DOCUMENT"


class RetentionPolicy(Base):
    __tablename__ = "retention_policies"
    __table_args__ = (
        CheckConstraint("chat_days > 0", name="ck_retention_policies_chat_days_positive"),
        CheckConstraint("email_days > 0", name="ck_retention_policies_email_days_positive"),
        CheckConstraint("audit_days > 0", name="ck_retention_policies_audit_days_positive"),
        CheckConstraint("version > 0", name="ck_retention_policies_version_positive"),
        UniqueConstraint("organization_id", name="uq_retention_policies_organization"),
    )

    # 数据保留策略的唯一标识。
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
    # 聊天数据允许保留的天数。
    chat_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default=text("90")
    )
    # 邮件数据允许保留的天数。
    email_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default=text("90")
    )
    # 审计事件允许保留的天数。
    audit_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default=text("365")
    )
    # 并发控制版本号，用于检测和阻止并发覆盖。
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @classmethod
    def default(cls, *, organization_id: UUID) -> "RetentionPolicy":
        return cls.configured(
            organization_id=organization_id,
            chat_days=90,
            email_days=90,
            audit_days=365,
        )

    @classmethod
    def configured(
        cls,
        *,
        organization_id: UUID,
        chat_days: int,
        email_days: int,
        audit_days: int,
    ) -> "RetentionPolicy":
        if min(chat_days, email_days, audit_days) <= 0:
            raise ValueError("retention periods must be positive")
        return cls(
            organization_id=organization_id,
            chat_days=chat_days,
            email_days=email_days,
            audit_days=audit_days,
            version=1,
        )


class ErasureRequest(Base):
    __tablename__ = "erasure_requests"
    __table_args__ = (
        CheckConstraint("replay_generation >= 0", name="ck_erasure_replay_generation_nonnegative"),
        Index(
            "ix_erasure_requests_subject",
            "organization_id",
            "subject_key_hash",
            "scope",
        ),
        Index("ix_erasure_requests_replay", "status", "replay_generation", "requested_at"),
    )

    # 数据擦除请求的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 提交数据擦除请求的员工唯一标识。
    requested_by_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 待擦除数据主体标识的不可逆哈希值。
    subject_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 数据擦除请求覆盖的数据范围。
    scope: Mapped[ErasureScope] = mapped_column(
        Enum(ErasureScope, name="erasure_scope"), nullable=False
    )
    # 记录当前业务状态。
    status: Mapped[ErasureStatus] = mapped_column(
        Enum(ErasureStatus, name="erasure_status"),
        nullable=False,
        default=ErasureStatus.PENDING,
        server_default=text("'PENDING'::erasure_status"),
    )
    # 数据擦除请求提交时间。
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 数据擦除实际完成时间；为空表示尚未应用。
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 备份恢复后重新应用擦除操作的代次。
    replay_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 擦除完成后各类目标的验证计数。
    verification_counts: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # 最近一次处理失败的标准错误码。
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ErasureTarget(Base):
    __tablename__ = "erasure_targets"
    __table_args__ = (
        UniqueConstraint(
            "request_id", "target_type", "target_id", name="uq_erasure_targets_identity"
        ),
        Index("ix_erasure_targets_request", "request_id", "target_type"),
    )

    # 数据擦除目标的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 所属数据擦除请求的唯一标识。
    request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("erasure_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 待擦除目标的资源类型。
    target_type: Mapped[ErasureTargetType] = mapped_column(
        Enum(ErasureTargetType, name="erasure_target_type"), nullable=False
    )
    # 待擦除目标的资源唯一标识。
    target_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
