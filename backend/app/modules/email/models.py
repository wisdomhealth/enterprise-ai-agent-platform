from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.jobs.models import JobIntent


class EmailState(StrEnum):
    INGESTED = "INGESTED"
    DRAFTING = "DRAFTING"
    DRAFT_RETRY_WAIT = "DRAFT_RETRY_WAIT"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPROVED = "APPROVED"
    SEND_PENDING = "SEND_PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    REJECTED = "REJECTED"
    SEND_RETRY_WAIT = "SEND_RETRY_WAIT"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    FAILED_TERMINAL = "FAILED_TERMINAL"


class EmailAction(StrEnum):
    START_DRAFT = "START_DRAFT"
    CLASSIFICATION_FAILED = "CLASSIFICATION_FAILED"
    CLASSIFIED_NO_DRAFT = "CLASSIFIED_NO_DRAFT"
    DRAFT_READY = "DRAFT_READY"
    DRAFT_FAILED = "DRAFT_FAILED"
    RETRY_DRAFT = "RETRY_DRAFT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SEND = "SEND"
    QUEUE_SEND = "QUEUE_SEND"
    CLAIM_SEND = "CLAIM_SEND"
    SEND_SUCCEEDED = "SEND_SUCCEEDED"
    SEND_FAILED = "SEND_FAILED"
    RETRY_SEND = "RETRY_SEND"
    DELIVERY_AMBIGUOUS = "DELIVERY_AMBIGUOUS"
    RECONCILE_SENT = "RECONCILE_SENT"
    RECONCILE_ABSENT = "RECONCILE_ABSENT"


class EmailCategory(StrEnum):
    ACTION_REQUIRED = "ACTION_REQUIRED"
    INFORMATIONAL = "INFORMATIONAL"
    SPAM = "SPAM"
    UNKNOWN = "UNKNOWN"


class EmailPriority(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class EmailWorkItem(Base):
    __tablename__ = "email_work_items"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "gmail_message_id", name="uq_email_work_items_org_message"
        ),
        CheckConstraint("version > 0", name="ck_email_work_items_version_positive"),
        Index("ix_email_work_items_queue", "organization_id", "state", "received_at"),
        Index("ix_email_work_items_connector", "connector_id", "received_at"),
        Index("ix_email_work_items_current_draft", "current_draft_id"),
    )

    # 邮件工作项的唯一标识。
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
    # 关联连接器的唯一标识。
    connector_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("connectors.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 所属知识库的唯一标识。
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Gmail 消息的外部唯一标识。
    gmail_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # Gmail 会话线程的外部唯一标识。
    gmail_thread_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # Gmail 增量同步历史位置。
    gmail_history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 邮件发件人地址。
    sender: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 邮件收件人地址列表。
    recipients: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 邮件主题。
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # 正文内容。
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 邮件接收时间。
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 原始邮件内容在外部存储中的引用。
    raw_content_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 记录当前状态机状态。
    state: Mapped[EmailState] = mapped_column(
        Enum(EmailState, name="email_state"),
        nullable=False,
        default=EmailState.INGESTED,
        server_default=text("'INGESTED'::email_state"),
    )
    # 邮件分类结果。
    category: Mapped[EmailCategory | None] = mapped_column(
        Enum(EmailCategory, name="email_category"), nullable=True
    )
    # 邮件优先级分类结果。
    priority: Mapped[EmailPriority | None] = mapped_column(
        Enum(EmailPriority, name="email_priority"), nullable=True
    )
    # 标记该邮件是否需要回复。
    reply_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 邮件分类结果的模型、提示词及证据来源信息。
    classification_provenance: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # 当前草稿正文；为空表示尚未生成草稿。
    draft_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 当前草稿引用的知识库证据。
    draft_citations: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # 当前草稿的模型、提示词和检索来源信息。
    draft_provenance: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # 最近一次处理失败的标准错误码。
    last_error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # 当前生效草稿版本的唯一标识。
    current_draft_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "email_draft_versions.id",
            name="fk_email_work_items_current_draft",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
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


class EmailDraftVersion(Base):
    __tablename__ = "email_draft_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_email_draft_versions_version_positive"),
        CheckConstraint(
            "creator_type IN ('SYSTEM', 'STAFF')", name="ck_email_draft_versions_creator_type"
        ),
        UniqueConstraint(
            "work_item_id", "version", name="uq_email_draft_versions_item_version"
        ),
        Index("ix_email_draft_versions_item_created", "work_item_id", "created_at"),
    )

    # 邮件草稿版本的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 关联邮件工作项的唯一标识。
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_work_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 草稿在所属邮件工作项中的递增版本号。
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 正文内容。
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 草稿的主收件人列表。
    to: Mapped[list[str]] = mapped_column("to_recipients", JSONB, nullable=False)
    # 草稿的抄送人列表。
    cc: Mapped[list[str]] = mapped_column(
        "cc_recipients", JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # 邮件主题。
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # 草稿回复的目标邮件线程标识。
    thread_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # 审核人员提供给草稿生成流程的补充指令。
    reviewer_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 生成或评测时使用的模型标识。
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # 生成或评测时使用的提示词版本。
    prompt_version: Mapped[str] = mapped_column(String(200), nullable=False)
    # 草稿生成时使用的检索配置快照。
    retrieval_config: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # 草稿或答案引用的来源证据。
    citations: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # 创建该草稿版本的主体唯一标识；具体主体类型由 creator_type 标识。
    created_by_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    # 创建该记录的主体类型。
    creator_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SYSTEM", server_default=text("'SYSTEM'")
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailApproval(Base):
    __tablename__ = "email_approvals"
    __table_args__ = (
        CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= approved_at",
            name="ck_email_approvals_invalidation_order",
        ),
        UniqueConstraint("draft_version_id", name="uq_email_approvals_draft_version"),
        Index("ix_email_approvals_item_active", "work_item_id", "invalidated_at"),
    )

    # 邮件审批记录的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 关联邮件工作项的唯一标识。
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_work_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 被批准草稿版本的唯一标识。
    draft_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_draft_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 执行审批的审核人员唯一标识。
    reviewer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("staff_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 草稿获批时间。
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 审批失效时间；为空表示仍然有效。
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryIntent(Base):
    __tablename__ = "email_delivery_intents"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_email_delivery_intents_version_positive"),
        UniqueConstraint(
            "approved_draft_version_id", name="uq_email_delivery_intents_approved_draft"
        ),
        UniqueConstraint("deterministic_message_id", name="uq_email_delivery_intents_message_id"),
        UniqueConstraint("job_id", name="uq_email_delivery_intents_job"),
        Index("ix_email_delivery_intents_queue", "organization_id", "state", "created_at"),
    )

    # 邮件投递意图的唯一标识。
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
    # 关联邮件工作项的唯一标识。
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_work_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 获准发送的草稿版本唯一标识。
    approved_draft_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_draft_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 支撑当前投递意图的审批记录唯一标识。
    approval_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_approvals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 关联持久化任务的唯一标识。
    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(JobIntent.id, ondelete="RESTRICT"),
        nullable=False,
    )
    # 为幂等发送生成的稳定邮件 Message-ID。
    deterministic_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # 记录当前状态机状态。
    state: Mapped[EmailState] = mapped_column(
        Enum(EmailState, name="email_state", create_type=False),
        nullable=False,
        default=EmailState.SEND_PENDING,
        server_default=text("'SEND_PENDING'::email_state"),
    )
    # 任务或投递已经尝试的次数。
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 最近一次处理失败的标准错误码。
    last_error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
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


class DeliveryAttempt(Base):
    __tablename__ = "email_delivery_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="ck_email_delivery_attempts_number_positive"),
        CheckConstraint(
            "outcome IN ('IN_PROGRESS', 'SENT', 'DEFINITIVE_FAILURE', 'UNKNOWN')",
            name="ck_email_delivery_attempts_outcome",
        ),
        UniqueConstraint(
            "delivery_intent_id", "attempt_number", name="uq_email_delivery_attempt_number"
        ),
        Index("ix_email_delivery_attempts_intent", "delivery_intent_id", "started_at"),
    )

    # 邮件投递尝试的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 关联邮件投递意图的唯一标识。
    delivery_intent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_delivery_intents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 当前投递尝试的序号。
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # 操作执行结果。
    outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, default="IN_PROGRESS", server_default=text("'IN_PROGRESS'")
    )
    # 本次处理失败的标准错误码。
    error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # 本次处理开始时间。
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 本次处理完成时间；为空表示尚未结束。
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SuccessfulDelivery(Base):
    __tablename__ = "email_successful_deliveries"
    __table_args__ = (
        UniqueConstraint("delivery_intent_id", name="uq_email_success_delivery_intent"),
        Index("ix_email_successful_deliveries_message", "gmail_message_id"),
    )

    # 成功投递记录的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 关联邮件投递意图的唯一标识。
    delivery_intent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_delivery_intents.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Gmail 消息的外部唯一标识。
    gmail_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # Gmail 会话线程的外部唯一标识。
    gmail_thread_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # 标记成功投递结果是否已完成核对。
    reconciled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 邮件确认发送成功的时间。
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailStateHistory(Base):
    __tablename__ = "email_state_history"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('SYSTEM', 'STAFF')", name="ck_email_state_history_actor_type"
        ),
        Index("ix_email_state_history_item", "work_item_id", "created_at"),
    )

    # 邮件状态变更历史的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 关联邮件工作项的唯一标识。
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("email_work_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所属组织的唯一标识，用于实施租户数据隔离。
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 状态转换前的状态。
    from_state: Mapped[EmailState] = mapped_column(
        Enum(EmailState, name="email_state", create_type=False), nullable=False
    )
    # 状态转换后的状态。
    to_state: Mapped[EmailState] = mapped_column(
        Enum(EmailState, name="email_state", create_type=False), nullable=False
    )
    # 触发当前记录的业务操作名称。
    action: Mapped[EmailAction] = mapped_column(
        Enum(EmailAction, name="email_action"), nullable=False
    )
    # 触发状态转换的标准原因码。
    reason_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # 执行当前操作的主体唯一标识。
    actor_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=True,
    )
    # 执行当前操作的主体类型。
    actor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SYSTEM", server_default=text("'SYSTEM'")
    )
    # 关联持久化任务的唯一标识。
    job_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(JobIntent.id, ondelete="SET NULL"),
        nullable=True,
    )
    # 状态转换时对应资源的并发控制版本。
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailSyncState(Base):
    __tablename__ = "email_sync_states"
    __table_args__ = (
        UniqueConstraint("connector_id", name="uq_email_sync_states_connector"),
        Index("ix_email_sync_states_organization", "organization_id"),
    )

    # 邮件同步状态的唯一标识。
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
    # 关联连接器的唯一标识。
    connector_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("connectors.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 已经确认处理的 Gmail 历史位置。
    history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 尚待继续处理的 Gmail 分页令牌。
    pending_page_token: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 最近一次处理失败的标准错误码。
    last_error_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # 最近一次成功处理的时间。
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EmailEvaluationRun(Base):
    __tablename__ = "email_evaluation_runs"
    __table_args__ = (
        CheckConstraint("macro_f1 >= 0 AND macro_f1 <= 1", name="ck_email_eval_macro_f1"),
        CheckConstraint(
            "structured_output_success >= 0 AND structured_output_success <= 1",
            name="ck_email_eval_structured_success",
        ),
        CheckConstraint(
            "category_macro_f1 IS NULL OR (category_macro_f1 >= 0 AND category_macro_f1 <= 1)",
            name="ck_email_eval_category_macro_f1",
        ),
        CheckConstraint(
            "priority_macro_f1 IS NULL OR (priority_macro_f1 >= 0 AND priority_macro_f1 <= 1)",
            name="ck_email_eval_priority_macro_f1",
        ),
        CheckConstraint(
            "reply_required_f1 IS NULL OR (reply_required_f1 >= 0 AND reply_required_f1 <= 1)",
            name="ck_email_eval_reply_required_f1",
        ),
        CheckConstraint(
            "exact_match_rate IS NULL OR (exact_match_rate >= 0 AND exact_match_rate <= 1)",
            name="ck_email_eval_exact_match_rate",
        ),
        CheckConstraint(
            "metrics_version <> 'email-classification-v2' OR "
            "(category_macro_f1 IS NOT NULL AND priority_macro_f1 IS NOT NULL AND "
            "reply_required_f1 IS NOT NULL AND exact_match_rate IS NOT NULL)",
            name="ck_email_eval_complete_v2_metrics",
        ),
        Index("ix_email_evaluation_runs_created", "created_at"),
    )

    # 邮件评测运行记录的唯一标识。
    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 评测数据集的版本标识。
    dataset_version: Mapped[str] = mapped_column(String(200), nullable=False)
    # 评测数据集的类型。
    dataset_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # 评测数据集内容摘要，用于确认输入未发生变化。
    dataset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # 生成或评测时使用的模型标识。
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # 生成或评测时使用的提示词版本。
    prompt_version: Mapped[str] = mapped_column(String(200), nullable=False)
    # 评测指标计算逻辑的版本。
    metrics_version: Mapped[str] = mapped_column(
        String(100), nullable=False, server_default=text("'email-category-only-v1'")
    )
    # 分类任务的宏平均 F1 分数。
    macro_f1: Mapped[float] = mapped_column(Float, nullable=False)
    # 邮件类别预测的宏平均 F1 分数。
    category_macro_f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 邮件优先级预测的宏平均 F1 分数。
    priority_macro_f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 是否需要回复预测的 F1 分数。
    reply_required_f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 全部结构化分类字段完全匹配的比例。
    exact_match_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 模型成功返回合法结构化结果的比例。
    structured_output_success: Mapped[float] = mapped_column(Float, nullable=False)
    # 评测请求的总延迟，单位为毫秒。
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # 评测消耗的输入令牌数量。
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # 评测生成的输出令牌数量。
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # 根据令牌用量估算的调用成本。
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
