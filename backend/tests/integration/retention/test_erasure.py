import pytest
from sqlalchemy import func, select

from app.modules.audit.models import AuditEvent
from app.modules.knowledge.models import Document, DocumentChunk, DocumentVersion
from app.modules.outbox.models import OutboxEvent
from app.modules.retention.models import ErasureRequest, ErasureScope, ErasureStatus
from app.modules.retention.service import ErasureService


@pytest.mark.asyncio
async def test_erasure_removes_customer_content_but_keeps_minimal_ledger(
    db_session, retention_context
) -> None:  # type: ignore[no-untyped-def]
    service = ErasureService(
        db_session,
        principal=retention_context["principal"](retention_context["admin"]),
        hash_key=b"task22-erasure-key",
    )
    request = await service.request("customer@example.test", ErasureScope.CUSTOMER)
    await service.apply(request.id)
    await db_session.commit()
    await db_session.refresh(request)
    await db_session.refresh(retention_context["chat_session"])
    await db_session.refresh(retention_context["chat_message"])
    await db_session.refresh(retention_context["handoff"])
    await db_session.refresh(retention_context["email_item"])
    await db_session.refresh(retention_context["draft"])

    assert request.status is ErasureStatus.APPLIED
    assert request.subject_key_hash
    assert request.subject_key_hash != "customer@example.test"
    assert not hasattr(request, "deleted_body")
    assert retention_context["chat_message"].body == ""
    assert retention_context["chat_session"].customer_name is None
    assert retention_context["chat_session"].customer_email is None
    assert retention_context["handoff"].snapshot == {"erased": True}
    assert retention_context["email_item"].body == ""
    assert retention_context["email_item"].sender == ""
    assert retention_context["email_item"].recipients == []
    assert retention_context["email_item"].classification_provenance == {}
    assert retention_context["email_item"].draft_body is None
    assert retention_context["draft"].body == ""
    assert retention_context["draft"].to == []
    assert retention_context["draft"].reviewer_instruction is None
    assert await db_session.get(DocumentChunk, retention_context["chunk"].id) is not None
    outbox = await db_session.scalar(
        select(OutboxEvent).where(OutboxEvent.event_id == retention_context["chat_outbox"].event_id)
    )
    assert outbox is not None
    assert outbox.payload == {"erased": True}
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "retention.erasure.apply", AuditEvent.object_id == request.id
        )
    )
    assert audit is not None
    evidence = f"{request.__dict__!r}{audit.details!r}".lower()
    assert "customer@example.test" not in evidence
    assert "private" not in evidence


@pytest.mark.asyncio
async def test_applying_same_erasure_request_is_idempotent(db_session, retention_context) -> None:  # type: ignore[no-untyped-def]
    service = ErasureService(
        db_session,
        principal=retention_context["principal"](retention_context["admin"]),
        hash_key=b"task22-erasure-key",
    )
    request = await service.request("customer@example.test", ErasureScope.CUSTOMER)
    await service.apply(request.id)
    first_counts = dict(request.verification_counts)
    await service.apply(request.id)

    assert request.verification_counts == first_counts
    assert (
        await db_session.scalar(select(ErasureRequest).where(ErasureRequest.id == request.id))
    ) is request


@pytest.mark.asyncio
async def test_explicit_authorized_knowledge_erasure_removes_document_derivatives(
    db_session, retention_context
) -> None:  # type: ignore[no-untyped-def]
    document = retention_context["document"]
    version = retention_context["version"]
    chunk = retention_context["chunk"]
    document_id = document.id
    version_id = version.id
    chunk_id = chunk.id
    service = ErasureService(
        db_session,
        principal=retention_context["principal"](retention_context["admin"]),
        hash_key=b"task22-erasure-key",
    )

    request = await service.request(str(document.id), ErasureScope.KNOWLEDGE_DOCUMENT)
    request_id = request.id
    await service.apply(request.id)
    await db_session.commit()

    db_session.expire_all()
    assert await db_session.get(Document, document_id) is None
    assert await db_session.get(DocumentVersion, version_id) is None
    assert await db_session.get(DocumentChunk, chunk_id) is None
    persisted_request = await db_session.get(ErasureRequest, request_id)
    assert persisted_request is not None
    assert persisted_request.verification_counts == {
        "chat_sessions": 0,
        "email_items": 0,
        "knowledge_documents": 1,
    }


@pytest.mark.asyncio
async def test_non_admin_cannot_create_erasure_request(retention_context) -> None:  # type: ignore[no-untyped-def]
    async with retention_context["client_for"](retention_context["reviewer"]) as client:
        response = await client.post(
            "/api/v1/admin/erasure-requests",
            headers={"Idempotency-Key": "reviewer-erasure"},
            json={"subject_ref": "customer@example.test", "scope": "CUSTOMER"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_erasure_api_replays_same_idempotency_key(db_session, retention_context) -> None:  # type: ignore[no-untyped-def]
    async with retention_context["client_for"](retention_context["admin"]) as client:
        first = await client.post(
            "/api/v1/admin/erasure-requests",
            headers={"Idempotency-Key": "customer-erasure-idempotency"},
            json={"subject_ref": "customer@example.test", "scope": "CUSTOMER"},
        )
        replay = await client.post(
            "/api/v1/admin/erasure-requests",
            headers={"Idempotency-Key": "customer-erasure-idempotency"},
            json={"subject_ref": "customer@example.test", "scope": "CUSTOMER"},
        )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json() == first.json()
    assert await db_session.scalar(select(func.count(ErasureRequest.id))) == 1
