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
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (
        UniqueConstraint("organization_id", "kind", name="uq_connectors_organization_kind"),
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
    kind: Mapped[ConnectorKind] = mapped_column(
        Enum(ConnectorKind, name="connector_kind"), nullable=False
    )
    status: Mapped[ConnectorStatus] = mapped_column(
        Enum(ConnectorStatus, name="connector_status"), nullable=False
    )
    secret_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("connector_secrets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
