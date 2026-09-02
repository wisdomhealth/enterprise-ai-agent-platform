from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.delivery import EmailDeliveryService
from app.modules.email.models import DeliveryAttempt, DeliveryIntent, EmailState
from app.modules.email.review import EmailReviewService
from app.modules.jobs.models import JobIntent, JobState


@pytest.mark.asyncio
async def test_definitive_failure_and_manual_retry_use_same_state_machine(
    db_session: AsyncSession,
    email_review_context: dict[str, object],
    delivery_gateway_factory,
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    await EmailReviewService(db_session, email_review_context["principal"]).approve(
        item.id, expected_version=item.version, current_draft_id=draft.id
    )
    intent = await db_session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.work_item_id == item.id)
    )
    assert intent is not None
    gateway = delivery_gateway_factory("definitive_failure")
    service = EmailDeliveryService(db_session, gateway, worker_id="worker-a")

    failed = await service.send(intent.job_id)
    assert failed.state is EmailState.SEND_RETRY_WAIT
    assert (await db_session.get(JobIntent, intent.job_id)).state is JobState.PENDING

    pending = await service.request_retry(intent.id, email_review_context["principal"])
    assert pending.state is EmailState.SEND_PENDING
    gateway.mode = "success"
    sent = await EmailDeliveryService(db_session, gateway, worker_id="worker-b").send(intent.job_id)

    assert sent.state is EmailState.SENT
    assert gateway.send_call_count == 2


@pytest.mark.asyncio
async def test_due_definitive_failure_uses_the_same_automatic_retry_state_machine(
    db_session: AsyncSession,
    email_review_context: dict[str, object],
    delivery_gateway_factory,
) -> None:
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    await EmailReviewService(db_session, email_review_context["principal"]).approve(
        item.id, expected_version=item.version, current_draft_id=draft.id
    )
    intent = await db_session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.work_item_id == item.id)
    )
    assert intent is not None
    gateway = delivery_gateway_factory("definitive_failure")
    failed = await EmailDeliveryService(
        db_session, gateway, worker_id="worker-a"
    ).send(intent.job_id)
    assert failed is not None and failed.state is EmailState.SEND_RETRY_WAIT
    job = await db_session.get(JobIntent, intent.job_id)
    assert job is not None
    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    gateway.mode = "success"
    sent = await EmailDeliveryService(db_session, gateway, worker_id="worker-b").send(
        intent.job_id
    )

    assert sent is not None and sent.state is EmailState.SENT
    assert gateway.send_call_count == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DeliveryAttempt)
            .where(
                DeliveryAttempt.delivery_intent_id == intent.id,
            )
        )
        == 2
    )
