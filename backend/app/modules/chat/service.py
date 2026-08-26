from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import (
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
        knowledge_base = await self.knowledge_base_for_public_key(public_key)
        if knowledge_base is None:
            return None
        await self._limiter().check_creation(
            ip_address=ip_address, organization_id=str(knowledge_base.organization_id)
        )
        return await self.create_session_for_knowledge_base(
            knowledge_base=knowledge_base,
            customer_name=customer_name,
            customer_email=customer_email,
        )

    async def knowledge_base_for_public_key(self, public_key: str) -> KnowledgeBase | None:
        knowledge_base = await self._db_session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.public_key == public_key)
        )
        return knowledge_base if isinstance(knowledge_base, KnowledgeBase) else None

    async def check_invalid_creation_attempt(self, *, ip_address: str) -> None:
        await self._limiter().check_creation_ip(ip_address=ip_address)

    async def check_creation_admission(
        self, *, ip_address: str, organization_id: UUID
    ) -> None:
        await self._limiter().check_creation(
            ip_address=ip_address, organization_id=str(organization_id)
        )

    async def check_rotation_admission(
        self, *, ip_address: str, session_id: UUID, organization_id: UUID
    ) -> None:
        await self._limiter().check_message(
            ip_address=ip_address,
            session_id=str(session_id),
            organization_id=str(organization_id),
        )

    async def create_session_for_knowledge_base(
        self,
        *,
        knowledge_base: KnowledgeBase,
        customer_name: str | None,
        customer_email: str | None,
    ) -> tuple[ChatSession, str, datetime]:
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

    def _limiter(self) -> SlidingWindowRateLimiter:
        if self._rate_limiter is None:
            from app.modules.chat.rate_limit import RateLimitUnavailable

            raise RateLimitUnavailable
        return self._rate_limiter

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
        self, *, session_id: UUID, credential_value: str
    ) -> tuple[str, datetime] | None:
        now = datetime.now(UTC)
        credential = await self._db_session.scalar(
            select(ChatSessionCredential)
            .where(
                ChatSessionCredential.session_id == session_id,
                ChatSessionCredential.token_hash == hash_chat_token(credential_value),
                ChatSessionCredential.revoked_at.is_(None),
                ChatSessionCredential.expires_at > now,
            )
            .with_for_update()
        )
        if not isinstance(credential, ChatSessionCredential):
            return None
        session = await self._db_session.scalar(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
        if not isinstance(session, ChatSession):
            return None
        if session.state is ConversationState.RESOLVED:
            raise ValueError("chat session is resolved")
        credential.revoked_at = now
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
