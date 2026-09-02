import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.delivery import EmailDeliveryService, ReconciliationRequired
from app.modules.email.models import DeliveryIntent, EmailState
from app.modules.email.reconciliation import ReconciliationService
from app.modules.email.review import EmailReviewService


async def _unknown(
    db_session: AsyncSession, context: dict[str, object], gateway_factory
) -> tuple[DeliveryIntent, object]:
    item = context["item"]
    draft = context["draft"]
    await EmailReviewService(db_session, context["principal"]).approve(
        item.id, expected_version=item.version, current_draft_id=draft.id
    )
    intent = await db_session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.work_item_id == item.id)
    )
    assert intent is not None
    gateway = gateway_factory("timeout_after_send")
    await EmailDeliveryService(db_session, gateway, worker_id="worker-a").send(intent.job_id)
    return intent, gateway


@pytest.mark.asyncio
async def test_delivery_unknown_blocks_automatic_and_manual_retry(
    db_session: AsyncSession,
    email_review_context: dict[str, object],
    delivery_gateway_factory,
) -> None:
    intent, gateway = await _unknown(db_session, email_review_context, delivery_gateway_factory)
    service = EmailDeliveryService(db_session, gateway, worker_id="worker-b")

    assert intent.state is EmailState.DELIVERY_UNKNOWN
    with pytest.raises(ReconciliationRequired):
        await service.send(intent.job_id)
    with pytest.raises(ReconciliationRequired):
        await service.request_retry(intent.id, email_review_context["principal"])
    assert gateway.send_call_count == 1


@pytest.mark.asyncio
async def test_authorized_absence_reconciliation_returns_to_send_pending(
    db_session: AsyncSession,
    email_review_context: dict[str, object],
    delivery_gateway_factory,
) -> None:
    intent, gateway = await _unknown(db_session, email_review_context, delivery_gateway_factory)
    gateway.sent.clear()

    result = await ReconciliationService(
        db_session, gateway, email_review_context["principal"]
    ).reconcile(intent.id, confirm_absent=True)

    assert result.state is EmailState.SEND_PENDING
    assert gateway.send_call_count == 1
