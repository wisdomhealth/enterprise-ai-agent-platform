import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditService
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole


def application_database_dsn() -> str:
    database_url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    return database_url.replace("postgres@", "platform_app@", 1)


@pytest.fixture
def principal() -> Principal:
    return Principal(
        subject_id=uuid4(),
        organization_id=uuid4(),
        email="audit-actor@example.com",
        role=UserRole.ADMIN,
        session_id=uuid4(),
        csrf_hash="audit-csrf-hash",
    )


@pytest.mark.asyncio
async def test_audit_service_records_safe_actor_and_scope_data(db_session, principal):
    service = AuditService()
    object_id = uuid4()

    event = await service.record(
        db_session,
        principal,
        action="support.claim",
        object_type="ticket",
        object_id=object_id,
        outcome="ALLOWED",
        details={"reason_code": "assigned", "secret": "must-not-be-recorded"},
        safe_detail_keys={"reason_code"},
    )
    await db_session.flush()

    stored = await db_session.scalar(select(AuditEvent).where(AuditEvent.id == event.id))
    assert stored is not None
    assert stored.organization_id == principal.organization_id
    assert stored.actor_id == principal.subject_id
    assert stored.action == "support.claim"
    assert stored.object_id == object_id
    assert stored.details == {"reason_code": "assigned"}


@pytest.mark.asyncio
async def test_application_role_can_insert_but_cannot_update_or_delete_audit_events():
    connection = await asyncpg.connect(application_database_dsn())
    event_id = uuid4()
    organization_id = uuid4()
    actor_id = uuid4()
    object_id = uuid4()
    try:
        await connection.execute(
            """
            INSERT INTO audit_events
                (id, organization_id, actor_id, action, object_type, object_id,
                 outcome, details)
            VALUES ($1, $2, $3, 'security.denied', 'job', $4, 'DENIED', '{}'::jsonb)
            """,
            event_id,
            organization_id,
            actor_id,
            object_id,
        )

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "UPDATE audit_events SET outcome = 'ALLOWED' WHERE id = $1", event_id
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute("DELETE FROM audit_events WHERE id = $1", event_id)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_application_role_cannot_modify_migration_metadata():
    connection = await asyncpg.connect(application_database_dsn())
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "UPDATE alembic_version SET version_num = version_num"
            )
    finally:
        await connection.close()
