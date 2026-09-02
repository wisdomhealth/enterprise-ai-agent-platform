import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker, engine
from app.modules.authorization.models import ResourceGrant
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorSecret, ConnectorStatus
from app.modules.email.models import EmailApproval, EmailDraftVersion, EmailState, EmailWorkItem
from app.modules.email.review import EmailReviewConflict, EmailReviewService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase


async def _seed_committed_review_item() -> tuple[UUID, UUID, UUID, Principal, UUID]:
    """Create the concurrency fixture through a committed independent session."""

    async with async_sessionmaker() as session:
        organization = Organization(name=f"Concurrent review {uuid4()}")
        session.add(organization)
        await session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id,
            public_key=f"concurrent-{uuid4().hex}",
        )
        secret = ConnectorSecret(
            organization_id=organization.id,
            ciphertext=b"fixture-ciphertext",
            encrypted_data_key=b"fixture-key",
            nonce=b"fixture-nonce",
            algorithm="AES-256-GCM",
            key_version="fixture",
        )
        session.add_all((knowledge_base, secret))
        await session.flush()
        connector = Connector(
            organization_id=organization.id,
            kind=ConnectorKind.GMAIL,
            status=ConnectorStatus.ACTIVE,
            secret_id=secret.id,
        )
        reviewer = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"reviewer-{uuid4()}",
            email="reviewer@example.test",
            role=UserRole.REVIEWER,
            status=UserStatus.ACTIVE,
        )
        session.add_all((connector, reviewer))
        await session.flush()
        session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=reviewer.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.review"],
            )
        )
        item = EmailWorkItem(
            organization_id=organization.id,
            connector_id=connector.id,
            knowledge_base_id=knowledge_base.id,
            gmail_message_id=f"concurrent-{uuid4().hex}",
            gmail_thread_id="thread-concurrent",
            sender="customer@example.test",
            recipients=["support@example.test"],
            subject="Need help",
            body="Please help with this request.",
            received_at=datetime(2026, 9, 2, tzinfo=UTC),
            raw_content_ref="gmail://fixture/concurrent",
            state=EmailState.AWAITING_REVIEW,
            draft_body="Original grounded draft.",
            draft_citations=[],
            draft_provenance={"model": "fixture", "prompt_version": "fixture"},
            version=1,
        )
        session.add(item)
        await session.flush()
        draft = EmailDraftVersion(
            work_item_id=item.id,
            organization_id=organization.id,
            version=1,
            body=item.draft_body,
            to=["customer@example.test"],
            cc=[],
            subject="Re: Need help",
            thread_id=item.gmail_thread_id,
            reviewer_instruction=None,
            model="fixture",
            prompt_version="fixture",
            retrieval_config={},
            citations=[],
            created_by_id=reviewer.id,
            creator_type="STAFF",
        )
        session.add(draft)
        await session.flush()
        item.current_draft_id = draft.id
        await session.commit()
    principal = Principal(
        reviewer.id,
        organization.id,
        reviewer.email,
        reviewer.role,
        uuid4(),
        "fixture-csrf",
    )
    return item.id, item.version, draft.id, principal, organization.id


@pytest.mark.asyncio
async def test_stale_approval_reports_current_state_version_and_draft(
    db_session: AsyncSession, email_review_context: dict[str, object]
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    service = EmailReviewService(db_session, email_review_context["principal"])
    edited = await service.edit(
        item.id,
        subject="Updated subject",
        expected_version=item.version,
        current_draft_id=draft.id,
    )

    with pytest.raises(EmailReviewConflict) as captured:
        await service.approve(
            item.id,
            expected_version=item.version - 1,
            current_draft_id=draft.id,
        )
    assert captured.value.state is edited.state
    assert captured.value.version == edited.version
    assert captured.value.current_draft_id == edited.current_draft_id


@pytest.mark.asyncio
async def test_concurrent_approvals_create_one_approval() -> None:
    work_item_id, version, draft_id, principal, organization_id = (
        await _seed_committed_review_item()
    )

    async def approve() -> object:
        async with async_sessionmaker() as session:
            try:
                result = await EmailReviewService(session, principal).approve(
                    work_item_id,
                    expected_version=version,
                    current_draft_id=draft_id,
                )
                await session.commit()
                return result
            except EmailReviewConflict as error:
                await session.rollback()
                return error

    try:
        results = await asyncio.gather(approve(), approve())
        async with async_sessionmaker() as verify:
            stored = await verify.get(EmailWorkItem, work_item_id)
            approvals = list(
                (
                    await verify.scalars(
                        EmailApproval.__table__.select().where(
                            EmailApproval.work_item_id == work_item_id
                        )
                    )
                ).all()
            )
            versions = list(
                (
                    await verify.scalars(
                        EmailDraftVersion.__table__.select().where(
                            EmailDraftVersion.work_item_id == work_item_id
                        )
                    )
                ).all()
            )
    finally:
        async with async_sessionmaker() as cleanup:
            await cleanup.execute(delete(Organization).where(Organization.id == organization_id))
            await cleanup.commit()
        await engine.dispose()
    assert sum(result.__class__.__name__ == "EmailReviewResult" for result in results) == 1
    assert sum(isinstance(result, EmailReviewConflict) for result in results) == 1
    assert stored is not None and stored.state is EmailState.APPROVED
    assert len(approvals) == 1
    assert len(versions) == 1
