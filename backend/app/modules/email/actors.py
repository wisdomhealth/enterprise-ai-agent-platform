from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.identity.dependencies import ServicePrincipal
from app.modules.identity.models import UserRole

EMAIL_SYSTEM_ACTOR_TYPE = "SYSTEM"


def email_worker_actor_id(organization_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"email-worker:{organization_id}")


def email_worker_principal(
    organization_id: UUID, knowledge_base_id: UUID, job_id: UUID
) -> ServicePrincipal:
    return ServicePrincipal(
        subject_id=email_worker_actor_id(organization_id),
        organization_id=organization_id,
        email="email-worker@system.invalid",
        role=UserRole.REVIEWER,
        session_id=job_id,
        csrf_hash="",
        resource_type="knowledge",
        resource_id=knowledge_base_id,
        purpose="email.draft",
    )
