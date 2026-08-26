from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import (
    ChatActor,
    ChatMessage,
    ChatMessageStatus,
    ChatSession,
    ChatSessionCredential,
    ConversationState,
)
from app.modules.chat.rate_limit import SlidingWindowRateLimiter
from app.modules.chat.tokens import ChatTokenService, hash_chat_token
from app.modules.knowledge.models import KnowledgeBase


class ChatSessionService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        token_service: ChatTokenService | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self._db_session = db_session
        self._token_service = token_service or ChatTokenService()
        self._rate_limiter = rate_limiter

    async def create_session(
        self,
        *,
        public_key: str,
        customer_name: str | None,
        customer_email: str | None,
        ip_address: str,
    ) -> tuple[ChatSession, str, datetime] | None:
        knowledge_base = await self._db_session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.public_key == public_key)
        )
        if knowledge_base is None:
            return None
        if self._rate_limiter is not None:
            await self._rate_limiter.check_creation(
                ip_address=ip_address, organization_id=str(knowledge_base.organization_id)
            )
        session = ChatSession(
            organization_id=knowledge_base.organization_id,
            knowledge_base_id=knowledge_base.id,
            customer_name=customer_name,
            customer_email=customer_email,
        )
        self._db_session.add(session)
        await self._db_session.flush()
        issued = self._token_service.issue(session_id=session.id)
        self._db_session.add(
            ChatSessionCredential(
                session_id=session.id,
                token_hash=issued.token_hash,
                expires_at=issued.expires_at,
            )
        )
        await self._db_session.flush()
        return session, issued.value, issued.expires_at

    async def get_authorized_session(
        self, *, session_id: UUID, credential_value: str
    ) -> ChatSession | None:
        now = datetime.now(UTC)
        session = await self._db_session.scalar(
            select(ChatSession)
            .join(ChatSessionCredential, ChatSessionCredential.session_id == ChatSession.id)
            .where(
                ChatSession.id == session_id,
                ChatSessionCredential.token_hash == hash_chat_token(credential_value),
                ChatSessionCredential.revoked_at.is_(None),
                ChatSessionCredential.expires_at > now,
            )
        )
        return session if isinstance(session, ChatSession) else None

    async def rotate_credential(
        self, *, session: ChatSession
    ) -> tuple[str, datetime] | None:
        if session.state is ConversationState.RESOLVED:
            return None
        now = datetime.now(UTC)
        await self._db_session.execute(
            update(ChatSessionCredential)
            .where(
                ChatSessionCredential.session_id == session.id,
                ChatSessionCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        issued = self._token_service.issue(session_id=session.id)
        self._db_session.add(
            ChatSessionCredential(
                session_id=session.id,
                token_hash=issued.token_hash,
                expires_at=issued.expires_at,
            )
        )
        await self._db_session.flush()
        return issued.value, issued.expires_at

    async def add_customer_message(
        self, *, session: ChatSession, body: str, ip_address: str
    ) -> ChatMessage:
        if session.state is ConversationState.RESOLVED:
            raise ValueError("chat session is resolved")
        if self._rate_limiter is not None:
            await self._rate_limiter.check_message(
                session_id=str(session.id),
                organization_id=str(session.organization_id),
                ip_address=ip_address,
            )
        next_sequence = await self._db_session.scalar(
            select(func.coalesce(func.max(ChatMessage.sequence), 0) + 1).where(
                ChatMessage.session_id == session.id
            )
        )
        message = ChatMessage(
            session_id=session.id,
            sequence=next_sequence if isinstance(next_sequence, int) else 1,
            actor=ChatActor.CUSTOMER,
            body=body,
            status=ChatMessageStatus.PERSISTED,
        )
        self._db_session.add(message)
        await self._db_session.flush()
        return message

    async def messages_for(self, *, session_id: UUID) -> list[ChatMessage]:
        return list(
            (
                await self._db_session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.sequence)
                )
            ).all()
        )
