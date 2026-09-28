from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
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


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RECONCILIATION = "RECONCILIATION"


class ErrorClass(StrEnum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    AMBIGUOUS = "AMBIGUOUS"
    SECURITY = "SECURITY"


class JobIntent(Base):
    __tablename__ = "job_intents"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_job_intents_attempts_nonnegative"),
        CheckConstraint("version > 0", name="ck_job_intents_version_positive"),
        UniqueConstraint("kind", "idempotency_key", name="uq_job_intents_kind_key"),
        Index(
            "ix_job_intents_claimable",
            "state",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )

    # 持久化任务意图的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 记录业务类型。
    kind: Mapped[str] = mapped_column(String(150), nullable=False)
    # 任务幂等键，用于防止同类工作重复创建。
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # 任务执行所需的结构化参数。
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # 记录当前状态机状态。
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state"),
        nullable=False,
        default=JobState.PENDING,
        server_default=text("'PENDING'::job_state"),
    )
    # 当前持有任务租约的 Worker 标识。
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 当前处理租约的过期时间。
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 任务或投递已经尝试的次数。
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 任务允许再次尝试的最早时间。
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 最近一次处理失败的标准错误码。
    last_error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # 最近一次失败的错误分类，用于决定重试策略。
    error_class: Mapped[ErrorClass | None] = mapped_column(
        Enum(ErrorClass, name="error_class"), nullable=True
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
