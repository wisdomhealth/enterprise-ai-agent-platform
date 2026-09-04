import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from app.core.database import async_sessionmaker, engine
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent
from app.modules.retention.models import (
    ErasureRequest,
    ErasureScope,
    ErasureTarget,
    RetentionPolicy,
)
from app.modules.retention.service import (
    ErasureService,
    RetentionConflict,
    RetentionService,
)


@dataclass(frozen=True, slots=True)
class _CommittedCustomer:
    organization_id: UUID
    chat_session_id: UUID
    chat_message_id: UUID
    principal: Principal


async def _seed_committed_customer() -> _CommittedCustomer:
    async with async_sessionmaker() as session:
        organization = Organization(name=f"Retention concurrency {uuid4()}")
        session.add(organization)
        await session.flush()
        admin = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"retention-concurrency-{uuid4()}",
            email="retention-concurrency@example.test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        knowledge_base = KnowledgeBase(
            organization_id=organization.id,
            public_key=f"retention-{uuid4().hex}",
        )
        session.add_all((admin, knowledge_base))
        await session.flush()
        chat_session = ChatSession(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
            customer_email="retention-customer@example.test",
        )
        session.add(chat_session)
        await session.flush()
        message = ChatMessage(
            session_id=chat_session.id,
            sequence=1,
            actor=ChatActor.CUSTOMER,
            body="durable private chat body",
        )
        session.add(message)
        await session.commit()
    return _CommittedCustomer(
        organization_id=organization.id,
        chat_session_id=chat_session.id,
        chat_message_id=message.id,
        principal=Principal(
            admin.id,
            organization.id,
            admin.email,
            admin.role,
            uuid4(),
            "retention-concurrency-csrf",
        ),
    )


async def _cleanup_committed_customer(context: _CommittedCustomer) -> None:
    async with async_sessionmaker() as session:
        await session.execute(
            delete(ErasureRequest).where(ErasureRequest.organization_id == context.organization_id)
        )
        await session.execute(
            delete(Organization).where(Organization.id == context.organization_id)
        )
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_policy_updates_accept_only_one_expected_version() -> None:
    context = await _seed_committed_customer()
    async with async_sessionmaker() as lookup:
        policy = await lookup.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.organization_id == context.organization_id
            )
        )
        assert policy is not None
        expected_version = policy.version

    async def update_policy(chat_days: int) -> object:
        async with async_sessionmaker() as session:
            try:
                updated = await RetentionService(session).update_policy(
                    context.principal,
                    expected_version=expected_version,
                    chat_days=chat_days,
                    email_days=90,
                    audit_days=365,
                )
                await session.commit()
                return updated
            except RetentionConflict as error:
                await session.rollback()
                return error

    try:
        results = await asyncio.gather(update_policy(30), update_policy(60))
        async with async_sessionmaker() as verify:
            persisted = await verify.scalar(
                select(RetentionPolicy).where(
                    RetentionPolicy.organization_id == context.organization_id
                )
            )
            assert persisted is not None
            assert persisted.version == expected_version + 1
            assert persisted.chat_days in {30, 60}
    finally:
        await _cleanup_committed_customer(context)

    assert sum(isinstance(result, RetentionPolicy) for result in results) == 1
    assert sum(isinstance(result, RetentionConflict) for result in results) == 1


@pytest.mark.asyncio
async def test_erasure_replay_recovers_from_durable_state_in_fresh_sessions() -> None:
    context = await _seed_committed_customer()
    request_id: UUID
    try:
        async with async_sessionmaker() as apply_session:
            service = ErasureService(
                apply_session,
                principal=context.principal,
                hash_key=b"task22-cross-session-key",
            )
            request = await service.request(
                "retention-customer@example.test", ErasureScope.CUSTOMER
            )
            request_id = request.id
            await service.apply(request_id, restore_generation=1)
            await apply_session.commit()

        async with async_sessionmaker() as restore_session:
            restored = await restore_session.get(ChatMessage, context.chat_message_id)
            request = await restore_session.get(ErasureRequest, request_id)
            assert restored is not None and request is not None
            restored.body = "body restored from an older backup"
            request.replay_generation = 1
            await restore_session.commit()

        async with async_sessionmaker() as replay_session:
            replayed = await ErasureService(
                replay_session, hash_key=b"replay-does-not-use-key"
            ).replay_pending_and_applied(restore_generation=2)
            await replay_session.commit()
            assert replayed == 1

        async with async_sessionmaker() as verify_session:
            message = await verify_session.get(ChatMessage, context.chat_message_id)
            request = await verify_session.get(ErasureRequest, request_id)
            assert message is not None and message.body == ""
            assert request is not None and request.replay_generation == 2
            assert await ErasureService(
                verify_session, hash_key=b"replay-does-not-use-key"
            ).replay_is_complete(restore_generation=2)
    finally:
        await _cleanup_committed_customer(context)


@pytest.mark.asyncio
async def test_concurrent_erasure_application_is_idempotent() -> None:
    context = await _seed_committed_customer()
    try:
        async with async_sessionmaker() as setup:
            request = await ErasureService(
                setup,
                principal=context.principal,
                hash_key=b"task22-concurrent-erasure-key",
            ).request("retention-customer@example.test", ErasureScope.CUSTOMER)
            request_id = request.id
            await setup.commit()

        async def apply_erasure() -> UUID:
            async with async_sessionmaker() as session:
                applied = await ErasureService(session, hash_key=b"replay-does-not-use-key").apply(
                    request_id
                )
                await session.commit()
                return applied.id

        applied_ids = await asyncio.gather(apply_erasure(), apply_erasure())
        async with async_sessionmaker() as verify:
            request_count = await verify.scalar(
                select(func.count(ErasureRequest.id)).where(
                    ErasureRequest.organization_id == context.organization_id
                )
            )
            target_count = await verify.scalar(
                select(func.count(ErasureTarget.id)).where(ErasureTarget.request_id == request_id)
            )
            job_count = await verify.scalar(
                select(func.count(JobIntent.id)).where(
                    JobIntent.kind == "retention.erasure.apply",
                    JobIntent.idempotency_key == f"retention-erasure:{request_id}",
                )
            )
            applied_event_count = await verify.scalar(
                select(func.count(OutboxEvent.event_id)).where(
                    OutboxEvent.event_type == "retention.erasure.applied",
                    OutboxEvent.aggregate_id == request_id,
                )
            )
            assert request_count == 1
            assert target_count == 1
            assert job_count == 1
            assert applied_event_count == 1
            assert (
                await verify.scalar(
                    select(ChatMessage.body).where(ChatMessage.id == context.chat_message_id)
                )
                == ""
            )
    finally:
        await _cleanup_committed_customer(context)

    assert applied_ids == [request_id, request_id]
