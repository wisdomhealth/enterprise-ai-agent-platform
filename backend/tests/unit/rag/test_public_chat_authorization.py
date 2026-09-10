from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.modules.chat.answering import _public_session_principal
from app.modules.chat.models import ChatSession
from app.modules.identity.dependencies import Principal, PublicChatPrincipal
from app.modules.identity.models import UserRole
from app.modules.rag.vector_search import _authorized_chunks_query


def _compile(principal: Principal, knowledge_base_id):  # type: ignore[no-untyped-def]
    return _authorized_chunks_query(principal, knowledge_base_id).compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )


def test_public_chat_scope_is_explicitly_bound_without_staff_resource_grant() -> None:
    organization_id = uuid4()
    knowledge_base_id = uuid4()
    principal = PublicChatPrincipal(
        subject_id=uuid4(),
        organization_id=organization_id,
        email="public-chat@invalid.local",
        role=UserRole.MEMBER,
        session_id=uuid4(),
        csrf_hash="",
        knowledge_base_id=knowledge_base_id,
    )

    compiled = _compile(principal, knowledge_base_id)

    assert "resource_grants" not in str(compiled)
    assert compiled.params["knowledge_base_id_1"] == knowledge_base_id
    assert compiled.params["organization_id_1"] == organization_id
    assert "document_versions.state" in str(compiled)
    assert "documents.current_version_id" in str(compiled)
    assert "drive_sources.status" in str(compiled)


def test_public_chat_scope_fails_closed_for_a_different_knowledge_base() -> None:
    principal = PublicChatPrincipal(
        subject_id=uuid4(),
        organization_id=uuid4(),
        email="public-chat@invalid.local",
        role=UserRole.MEMBER,
        session_id=uuid4(),
        csrf_hash="",
        knowledge_base_id=uuid4(),
    )

    compiled = _compile(principal, uuid4())

    assert "false" in str(compiled).lower()


def test_regular_member_principal_still_requires_a_resource_grant() -> None:
    principal = Principal(
        subject_id=uuid4(),
        organization_id=uuid4(),
        email="member@example.test",
        role=UserRole.MEMBER,
        session_id=uuid4(),
        csrf_hash="csrf",
    )

    compiled = _compile(principal, uuid4())

    assert "resource_grants" in str(compiled)


def test_authenticated_chat_session_creates_a_distinct_bound_public_principal() -> None:
    session = ChatSession(
        id=uuid4(),
        organization_id=uuid4(),
        knowledge_base_id=uuid4(),
    )

    principal = _public_session_principal(session)

    assert isinstance(principal, PublicChatPrincipal)
    assert principal.subject_id == session.id
    assert principal.session_id == session.id
    assert principal.organization_id == session.organization_id
    assert principal.knowledge_base_id == session.knowledge_base_id
