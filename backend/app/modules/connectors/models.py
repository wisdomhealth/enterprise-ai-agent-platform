from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, LargeBinary, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ConnectorKind(StrEnum):
    DRIVE = "DRIVE"
    GMAIL = "GMAIL"


class ConnectorStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    ERROR = "ERROR"


class ConnectorSecret(Base):
    __tablename__ = "connector_secrets"

    # 连接器密钥的唯一标识。
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
    # 使用数据密钥加密后的密文。
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 由主密钥封装后的数据加密密钥。
    encrypted_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 加密算法使用的随机数，解密时必须保持一致。
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 密文使用的加密算法标识。
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    # 加密主密钥版本，用于密钥轮换和历史数据解密。
    key_version: Mapped[str] = mapped_column(String(512), nullable=False)
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (
        UniqueConstraint("organization_id", "kind", name="uq_connectors_organization_kind"),
    )

    # 连接器的唯一标识。
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
    # 记录业务类型。
    kind: Mapped[ConnectorKind] = mapped_column(
        Enum(ConnectorKind, name="connector_kind"), nullable=False
    )
    # 记录当前业务状态。
    status: Mapped[ConnectorStatus] = mapped_column(
        Enum(ConnectorStatus, name="connector_status"), nullable=False
    )
    # 连接器所使用加密凭据的唯一标识。
    secret_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("connector_secrets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 记录创建时间，由数据库在插入时生成。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 记录最近更新时间，由数据库在更新时维护。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
