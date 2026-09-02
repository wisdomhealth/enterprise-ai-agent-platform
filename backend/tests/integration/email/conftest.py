from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.email.gmail_gateway import (
    GmailAmbiguousDeliveryError,
    GmailDefinitiveDeliveryError,
    GmailMessage,
    GmailSendResult,
    GmailSentMessage,
)
from app.modules.email.models import EmailDraftVersion, EmailState, EmailWorkItem
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase


class FakeDeliveryGateway:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.send_call_count = 0
        self.sent: dict[str, GmailSentMessage] = {}

    async def send_raw(self, raw_message: bytes, *, thread_id: str) -> GmailSendResult:
        self.send_call_count += 1
        parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
        message_id = str(parsed["Message-ID"])
        result = GmailSentMessage(
            gmail_message_id=f"sent-{self.send_call_count}",
            gmail_thread_id=thread_id,
            deterministic_message_id=message_id,
        )
        if self.mode == "definitive_failure":
            raise GmailDefinitiveDeliveryError("GMAIL_RATE_LIMITED")
        self.sent[message_id] = result
        if self.mode == "timeout_after_send":
            raise GmailAmbiguousDeliveryError("GMAIL_RESPONSE_TIMEOUT")
        return GmailSendResult(result.gmail_message_id, result.gmail_thread_id)

    async def find_sent(
        self,
        *,
        deterministic_message_id: str,
        thread_id: str,
        recipients: tuple[str, ...],
        sent_after: object,
        sent_before: object,
    ) -> GmailSentMessage | None:
        del recipients, sent_after, sent_before
        result = self.sent.get(deterministic_message_id)
        if result is None or result.gmail_thread_id != thread_id:
            return None
        return result


@pytest.fixture
def delivery_gateway_factory():  # type: ignore[no-untyped-def]
    return FakeDeliveryGateway


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


@pytest_asyncio.fixture
async def email_review_context(
    db_session: AsyncSession, email_context: dict[str, object]
) -> AsyncIterator[dict[str, object]]:
    organization = email_context["organization"]
    knowledge_base = email_context["knowledge_base"]
    connector = email_context["connector"]
    staff_user = email_context["staff_user"]
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=staff_user.id,
            resource_type="knowledge",
            resource_id=knowledge_base.id,
            actions=["knowledge.review"],
        )
    )
    item = EmailWorkItem(
        organization_id=organization.id,
        connector_id=connector.id,
        knowledge_base_id=knowledge_base.id,
        gmail_message_id=f"review-{uuid4().hex}",
        gmail_thread_id="thread-review-1",
        sender="customer@example.test",
        recipients=["support@example.test"],
        subject="Need help",
        body="Please help with this request.",
        received_at=datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
        raw_content_ref="gmail://fixture/review",
        state=EmailState.AWAITING_REVIEW,
        draft_body="Original grounded draft.",
        draft_citations=[],
        draft_provenance={
            "model": "claude-fixture",
            "prompt_version": "email-draft-v1",
            "retrieval_chunk_ids": [],
            "retrieval_document_version_ids": [],
            "retrieval_latency_ms": 1,
            "model_latency_ms": 1,
            "end_to_end_latency_ms": 2,
            "input_tokens": 10,
            "output_tokens": 4,
            "estimated_cost": 0.0,
            "retrieval_principal_id": str(staff_user.id),
            "retrieval_actor_type": "STAFF",
        },
        version=3,
    )
    db_session.add(item)
    await db_session.flush()
    draft = EmailDraftVersion(
        work_item_id=item.id,
        organization_id=item.organization_id,
        version=1,
        body=item.draft_body,
        to=["customer@example.test"],
        cc=[],
        subject="Re: Need help",
        thread_id=item.gmail_thread_id,
        reviewer_instruction=None,
        model="claude-fixture",
        prompt_version="email-draft-v1",
        retrieval_config={"retrieval_chunk_ids": []},
        citations=[],
        created_by_id=staff_user.id,
        creator_type="STAFF",
    )
    db_session.add(draft)
    await db_session.flush()
    item.current_draft_id = draft.id
    await db_session.flush()
    principal = Principal(
        staff_user.id,
        organization.id,
        staff_user.email,
        staff_user.role,
        uuid4(),
        "fixture-csrf",
    )
    yield {
        **email_context,
        "item": item,
        "draft": draft,
        "principal": principal,
    }
