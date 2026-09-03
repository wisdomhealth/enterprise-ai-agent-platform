from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.email.models import EmailDraftVersion, EmailState, EmailWorkItem
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.outbox.models import OutboxEvent
from app.modules.retention.models import RetentionPolicy
from app.modules.support.models import Handoff, HandoffTrigger


@pytest_asyncio.fixture
async def retention_context(db_session: AsyncSession) -> AsyncIterator[dict[str, object]]:
    now = datetime(2026, 9, 3, 8, tzinfo=UTC)
    old = now - timedelta(days=400)
    organization = Organization(name=f"Retention {uuid4()}")
    other_organization = Organization(name=f"Other retention {uuid4()}")
    db_session.add_all((organization, other_organization))
    await db_session.flush()
    admin = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"retention-admin-{uuid4()}",
        email="retention-admin@example.test",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    reviewer = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"retention-reviewer-{uuid4()}",
        email="retention-reviewer@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add_all((admin, reviewer))
    await db_session.flush()
    policy = await db_session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.organization_id == organization.id)
    )
    assert policy is not None
    knowledge_base = KnowledgeBase(organization_id=organization.id, public_key=uuid4().hex)
    db_session.add(knowledge_base)
    await db_session.flush()
    db_session.add_all(
        (
            ResourceGrant(
                organization_id=organization.id,
                subject_id=admin.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.write"],
            ),
        )
    )
    session_record = StaffSession(
        user_id=admin.id,
        csrf_hash="unused",
        expires_at=now + timedelta(hours=1),
    )
    db_session.add(session_record)
    chat_session = ChatSession(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        customer_name="Customer Name",
        customer_email="customer@example.test",
        created_at=old,
        updated_at=old,
    )
    db_session.add(chat_session)
    await db_session.flush()
    chat_message = ChatMessage(
        session_id=chat_session.id,
        sequence=1,
        actor=ChatActor.CUSTOMER,
        body="private chat body",
        created_at=old,
    )
    handoff = Handoff(
        session_id=chat_session.id,
        organization_id=organization.id,
        state=chat_session.state,
        trigger=HandoffTrigger.CUSTOMER_REQUEST,
        snapshot={"summary": "private summary", "customer": {"email": "customer@example.test"}},
        last_customer_sequence=1,
        created_at=old,
        updated_at=old,
    )
    db_session.add_all((chat_message, handoff))
    await db_session.flush()
    chat_outbox = OutboxEvent(
        event_type="chat.answer.validated",
        aggregate_type="chat_session",
        aggregate_id=chat_session.id,
        payload={
            "message_id": str(chat_message.id),
            "sequence": 1,
            "segments": ["private chat body"],
            "citations": [],
        },
        occurred_at=old,
    )
    secret = ConnectorSecret(
        organization_id=organization.id,
        ciphertext=b"ciphertext",
        encrypted_data_key=b"data-key",
        nonce=b"nonce",
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
    await db_session.flush()
    email_item = EmailWorkItem(
        organization_id=organization.id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
        gmail_message_id=f"retention-{uuid4().hex}",
        gmail_thread_id="thread-private",
        sender="customer@example.test",
        recipients=["support@example.test"],
        subject="private subject",
        body="private email body",
        received_at=old,
        raw_content_ref="gmail://private-message",
        state=EmailState.AWAITING_REVIEW,
        classification_provenance={"summary": "private derived classification"},
        draft_body="private generated draft",
        draft_citations=[{"private": "citation"}],
        draft_provenance={"private": "provenance"},
    )
    db_session.add(email_item)
    await db_session.flush()
    draft = EmailDraftVersion(
        work_item_id=email_item.id,
        organization_id=organization.id,
        version=1,
        body="private generated draft",
        to=["customer@example.test"],
        cc=[],
        subject="Re: private subject",
        thread_id="thread-private",
        reviewer_instruction="private instruction",
        model="model-id",
        prompt_version="prompt-id",
        retrieval_config={"summary": "private retrieval"},
        citations=[{"private": "citation"}],
        creator_type="SYSTEM",
        created_at=old,
    )
    db_session.add(draft)
    await db_session.flush()
    email_item.current_draft_id = draft.id
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="authorized-root",
        allowed_descendant_ids=[],
        connection_identity="drive-reader@example.test",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id="drive-document-1",
        title="Knowledge title",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    version = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.RETRIEVABLE,
        content_sha256="1" * 64,
        created_at=old,
    )
    db_session.add(version)
    await db_session.flush()
    chunk = DocumentChunk(
        id=uuid4(),
        document_version_id=version.id,
        ordinal=0,
        text="knowledge content must survive default retention",
        token_count=7,
        metadata_={},
        created_at=old,
    )
    db_session.add(chunk)
    document.current_version_id = version.id
    old_audit = AuditEvent(
        organization_id=organization.id,
        actor_id=admin.id,
        action="old.audit",
        object_type="fixture",
        object_id=uuid4(),
        outcome="SUCCESS",
        details={"safe": True},
        occurred_at=old,
    )
    recent_audit = AuditEvent(
        organization_id=organization.id,
        actor_id=admin.id,
        action="recent.audit",
        object_type="fixture",
        object_id=uuid4(),
        outcome="SUCCESS",
        details={"safe": True},
        occurred_at=now - timedelta(days=1),
    )
    db_session.add_all((chat_outbox, old_audit, recent_audit))
    await db_session.commit()

    def principal(user: StaffUser) -> Principal:
        return Principal(
            subject_id=user.id,
            organization_id=user.organization_id,
            email=user.email,
            role=user.role,
            session_id=session_record.id,
            csrf_hash="csrf",
        )

    @asynccontextmanager
    async def client_for(user: StaffUser) -> AsyncIterator[httpx.AsyncClient]:
        application = create_app(
            Settings.model_validate(
                {"SESSION_SECRET": "task22-session", "ERASURE_HASH_KEY": "task22-key"}
            )
        )
        resolved = principal(user)

        async def override_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        async def override_principal() -> Principal:
            return resolved

        application.dependency_overrides[get_db_session] = override_db
        application.dependency_overrides[require_staff_session] = override_principal
        application.dependency_overrides[require_staff_csrf] = override_principal
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="https://testserver",
                headers={"X-CSRF-Token": "csrf"},
            ) as client:
                yield client
        finally:
            application.dependency_overrides.clear()

    yield {
        "now": now,
        "organization": organization,
        "admin": admin,
        "reviewer": reviewer,
        "policy": policy,
        "principal": principal,
        "client_for": client_for,
        "chat_session": chat_session,
        "chat_message": chat_message,
        "handoff": handoff,
        "chat_outbox": chat_outbox,
        "email_item": email_item,
        "draft": draft,
        "document": document,
        "version": version,
        "chunk": chunk,
        "old_audit": old_audit,
        "recent_audit": recent_audit,
    }
