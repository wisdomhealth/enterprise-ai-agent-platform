import pytest

from app.modules.retention.models import ErasureScope
from app.modules.retention.service import ErasureService


@pytest.mark.asyncio
async def test_restored_content_is_deleted_by_ledger_replay(db_session, retention_context) -> None:  # type: ignore[no-untyped-def]
    service = ErasureService(
        db_session,
        principal=retention_context["principal"](retention_context["admin"]),
        hash_key=b"task22-erasure-key",
    )
    request = await service.request("customer@example.test", ErasureScope.CUSTOMER)
    await service.apply(request.id)
    await db_session.commit()

    # Model a restored older row while retaining the durable target identities.
    retention_context["chat_message"].body = "restored private chat"
    retention_context["email_item"].body = "restored private email"
    request.replay_generation = 1
    await db_session.commit()

    replayed = await ErasureService(
        db_session, hash_key=b"task22-erasure-key"
    ).replay_pending_and_applied(restore_generation=2)
    await db_session.commit()

    assert replayed == 1
    assert retention_context["chat_message"].body == ""
    assert retention_context["email_item"].body == ""
    assert request.replay_generation == 2
    assert await service.replay_is_complete(restore_generation=2)


@pytest.mark.asyncio
async def test_replay_check_fails_closed_until_every_request_is_current(
    db_session, retention_context
) -> None:  # type: ignore[no-untyped-def]
    service = ErasureService(
        db_session,
        principal=retention_context["principal"](retention_context["admin"]),
        hash_key=b"task22-erasure-key",
    )
    await service.request("customer@example.test", ErasureScope.CUSTOMER)

    assert not await service.replay_is_complete(restore_generation=3)

    await service.replay_pending_and_applied(restore_generation=3)
    assert await service.replay_is_complete(restore_generation=3)
