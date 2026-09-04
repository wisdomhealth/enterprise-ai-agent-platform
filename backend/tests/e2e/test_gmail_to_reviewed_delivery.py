from email import policy
from email.parser import BytesParser

import pytest
from sqlalchemy import select

from app.modules.email.delivery import EmailDeliveryService, ReconciliationRequired
from app.modules.email.gmail_gateway import GmailAmbiguousDeliveryError, GmailSendResult
from app.modules.email.models import DeliveryIntent, EmailState
from app.modules.email.review import EmailReviewService


class ProviderGateway:
    def __init__(self, provider_stack, *, timeout: bool = False) -> None:  # type: ignore[no-untyped-def]
        self._providers = provider_stack
        self._timeout = timeout

    async def send_raw(self, raw_message: bytes, *, thread_id: str) -> GmailSendResult:
        parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
        identity = str(parsed["Message-ID"])
        raw_identity = identity.encode("ascii").hex()
        async with self._providers.client("gmail") as gmail:
            try:
                response = await gmail.post(
                    "/gmail/v1/users/me/messages/send",
                    json={"raw": raw_identity, "threadId": thread_id},
                    headers={"X-Fake-Send-Then-Timeout": "1"} if self._timeout else {},
                )
            except Exception as error:
                raise GmailAmbiguousDeliveryError("GMAIL_RESPONSE_TIMEOUT") from error
        response.raise_for_status()
        payload = response.json()
        return GmailSendResult(payload["id"], payload["threadId"])


@pytest.mark.asyncio
async def test_unapproved_and_unknown_delivery_states_cannot_send(
    db_session, email_review_context, provider_stack
) -> None:  # type: ignore[no-untyped-def]
    item = email_review_context["item"]
    draft = email_review_context["draft"]
    principal = email_review_context["principal"]
    assert item.state is EmailState.AWAITING_REVIEW
    assert (
        await db_session.scalar(
            select(DeliveryIntent).where(DeliveryIntent.work_item_id == item.id)
        )
        is None
    )
    assert provider_stack.call_count("gmail") == 0

    await EmailReviewService(db_session, principal).approve(
        item.id, expected_version=item.version, current_draft_id=draft.id
    )
    intent = await db_session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.work_item_id == item.id)
    )
    assert intent is not None
    await EmailDeliveryService(
        db_session, ProviderGateway(provider_stack, timeout=True), worker_id="task26-worker"
    ).send(intent.job_id)
    assert intent.state is EmailState.DELIVERY_UNKNOWN

    with pytest.raises(ReconciliationRequired):
        await EmailDeliveryService(
            db_session, ProviderGateway(provider_stack), worker_id="task26-retry"
        ).send(intent.job_id)
    assert provider_stack.call_count("gmail", "/gmail/v1/users/me/messages/send") == 1
