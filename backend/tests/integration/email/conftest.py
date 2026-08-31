from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.models import (
    Connector,
    ConnectorKind,
    ConnectorSecret,
    ConnectorStatus,
)
from app.modules.email.gmail_gateway import GmailMessage
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase


@pytest_asyncio.fixture
async def email_context(
    db_session: AsyncSession, tmp_path: Path
) -> AsyncIterator[dict[str, object]]:
    organization = Organization(name=f"Email owner {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"email-{uuid4().hex}",
    )
    db_session.add(knowledge_base)
    secret = ConnectorSecret(
        organization_id=organization.id,
        ciphertext=b"fixture-ciphertext",
        encrypted_data_key=b"fixture-key",
        nonce=b"fixture-nonce",
        algorithm="AES-256-GCM",
        key_version="fixture",
    )
    db_session.add(secret)
    await db_session.flush()
    connector = Connector(
        organization_id=organization.id,
        kind=ConnectorKind.GMAIL,
        status=ConnectorStatus.ACTIVE,
        secret_id=secret.id,
    )
    db_session.add(connector)
    staff_user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"email-worker-{uuid4()}",
        email="reviewer@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(staff_user)
    await db_session.flush()
    await db_session.commit()
    message = GmailMessage(
        id=f"gmail-{uuid4().hex}",
        thread_id="thread-7",
        history_id="101",
        sender=" Customer <CUSTOMER@Example.Test> ",
        recipients=("Support <SUPPORT@Example.Test>", "other@example.test"),
        subject="  Need   refund help ",
        body="First line.\r\n\r\nSecond line.  ",
        received_at=datetime(2026, 8, 31, 8, 30, tzinfo=UTC),
        raw_content_ref="gmail://fixture/message",
    )
    yield {
        "organization": organization,
        "knowledge_base": knowledge_base,
        "connector": connector,
        "staff_user": staff_user,
        "message": message,
        "tmp_path": tmp_path,
    }
