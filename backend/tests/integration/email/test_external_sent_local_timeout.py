import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.delivery import EmailDeliveryService, ReconciliationRequired
from app.modules.email.models import DeliveryIntent, EmailState, SuccessfulDelivery
from app.modules.email.reconciliation import ReconciliationService
from app.modules.email.review import EmailReviewService


@pytest.mark.asyncio
async def test_external_send_then_local_timeout_never_resends(
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
    gateway = delivery_gateway_factory("timeout_after_send")
    sender = EmailDeliveryService(db_session, gateway, worker_id="worker-a")

    await sender.send(intent.job_id)
    assert intent.state is EmailState.DELIVERY_UNKNOWN
    with pytest.raises(ReconciliationRequired):
        await sender.send(intent.job_id)
    reconciled = await ReconciliationService(
        db_session, gateway, email_review_context["principal"]
    ).reconcile(intent.id)

    assert reconciled.state is EmailState.SENT
    assert gateway.send_call_count == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SuccessfulDelivery)
            .where(SuccessfulDelivery.delivery_intent_id == intent.id)
        )
        == 1
    )
