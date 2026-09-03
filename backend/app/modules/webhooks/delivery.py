from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from hashlib import sha256
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.authorization.models import ResourceGrant
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.connectors.encryption import EncryptedSecret, EnvelopeCipher
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService
from app.modules.outbox.models import OutboxEvent
from app.modules.outbox.service import OutboxService
from app.modules.webhooks.models import (
    WebhookDelivery,
    WebhookDeliveryState,
    WebhookSubscription,
    WebhookSubscriptionStatus,
)
from app.modules.webhooks.signing import WebhookSigner

EVENT_DATA_FIELDS = MappingProxyType(
    {
        "connector.authorized": frozenset({"organization_id", "kind"}),
        "connector.revoked": frozenset({"organization_id"}),
        "connector.reauthorization_required": frozenset({"organization_id", "kind", "error_code"}),
        "knowledge.drive_source.sync.requested": frozenset({"organization_id", "source_id"}),
        "knowledge.document.parse.requested": frozenset(
            {"organization_id", "source_id", "document_id"}
        ),
        "knowledge.document.cleanup.requested": frozenset(
            {"organization_id", "source_id", "document_id"}
        ),
        "support.handoff.queued": frozenset(
            {
                "organization_id",
                "handoff_id",
                "session_id",
                "state",
                "trigger",
                "last_customer_sequence",
            }
        ),
        "support.handoff.claimed": frozenset({"organization_id", "session_id", "assigned_user_id"}),
        "support.handoff.replied": frozenset({"organization_id", "message_id", "sequence"}),
        "support.handoff.resolved": frozenset({"organization_id", "session_id"}),
        "support.handoff.ai_resumed": frozenset(
            {"organization_id", "session_id", "handoff_boundary", "await_customer_message"}
        ),
        "email.draft.ready": frozenset({"organization_id", "state", "version", "draft_version_id"}),
        "email.delivery.sent": frozenset(
            {
                "organization_id",
                "delivery_intent_id",
                "approved_draft_version_id",
                "state",
            }
        ),
        "email.delivery.unknown": frozenset(
            {"organization_id", "delivery_intent_id", "state", "error_code"}
        ),
        "retention.erasure.applied": frozenset(
            {"organization_id", "scope", "status", "replay_generation"}
        ),
    }
)

EVENT_REQUIRED_FIELDS = MappingProxyType(
    {
        "connector.authorized": frozenset({"organization_id", "kind"}),
        "connector.revoked": frozenset({"organization_id"}),
        "connector.reauthorization_required": frozenset({"organization_id", "kind"}),
        "knowledge.drive_source.sync.requested": frozenset({"organization_id", "source_id"}),
        "knowledge.document.parse.requested": frozenset(
            {"organization_id", "source_id", "document_id"}
        ),
        "knowledge.document.cleanup.requested": frozenset(
            {"organization_id", "source_id", "document_id"}
        ),
        "support.handoff.queued": frozenset(
            {"organization_id", "handoff_id", "trigger", "last_customer_sequence"}
        ),
        "support.handoff.claimed": frozenset({"organization_id", "session_id", "assigned_user_id"}),
        "support.handoff.replied": frozenset({"organization_id", "message_id", "sequence"}),
        "support.handoff.resolved": frozenset({"organization_id", "session_id"}),
        "support.handoff.ai_resumed": frozenset(
            {"organization_id", "session_id", "handoff_boundary", "await_customer_message"}
        ),
        "email.draft.ready": frozenset({"organization_id", "state", "version", "draft_version_id"}),
        "email.delivery.sent": frozenset(
            {
                "organization_id",
                "delivery_intent_id",
                "approved_draft_version_id",
                "state",
            }
        ),
        "email.delivery.unknown": frozenset(
            {"organization_id", "delivery_intent_id", "state", "error_code"}
        ),
        "retention.erasure.applied": frozenset(
            {"organization_id", "scope", "status", "replay_generation"}
        ),
    }
)


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    body: dict[str, object]
    body_bytes: bytes
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class WebhookTransportResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] | None = None


class WebhookTransport(Protocol):
    async def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> WebhookTransportResponse: ...


class HttpxWebhookTransport:
    async def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> WebhookTransportResponse:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(15.0),
        ) as client:
            response = await client.post(url, content=body, headers=headers)
        return WebhookTransportResponse(
            status_code=response.status_code,
            body=response.content,
            headers=response.headers,
        )


def _validated_endpoint_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}
    ):
        raise ValueError("webhook endpoint must be an HTTPS URL without credentials or fragments")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("webhook endpoint must use an ASCII URL") from exc
    if len(encoded) > 2048:
        raise ValueError("webhook endpoint is too long")
    return value


class WebhookSubscriptionService:
    def __init__(
        self,
        db_session: AsyncSession,
        cipher: EnvelopeCipher | None,
        *,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._cipher = cipher
        self._audit = audit_service or AuditService()
        self._outbox = outbox_service or OutboxService()

    async def create(
        self,
        principal: Principal,
        *,
        endpoint_url: str,
        event_types: list[str],
        signing_secret: str,
        subscription_id: UUID | None = None,
    ) -> WebhookSubscription:
        if principal.role is not UserRole.ADMIN:
            raise LookupError("webhook subscription not found")
        normalized_types = sorted(set(event_types))
        if not normalized_types or any(kind not in EVENT_DATA_FIELDS for kind in normalized_types):
            raise ValueError("webhook event type is not allowed")
        if len(signing_secret.encode("utf-8")) < 32:
            raise ValueError("webhook signing secret must be at least 32 bytes")
        if self._cipher is None:
            raise RuntimeError("webhook envelope encryption is not configured")
        encrypted = await self._cipher.encrypt(signing_secret)
        subscription = WebhookSubscription(
            organization_id=principal.organization_id,
            created_by_id=principal.subject_id,
            endpoint_url=_validated_endpoint_url(endpoint_url),
            event_types=normalized_types,
            secret_ciphertext=encrypted.ciphertext,
            secret_encrypted_data_key=encrypted.encrypted_data_key,
            secret_nonce=encrypted.nonce,
            secret_algorithm=encrypted.algorithm,
            secret_key_version=encrypted.key_version,
        )
        if subscription_id is not None:
            subscription.id = subscription_id
        self._db_session.add(subscription)
        await self._db_session.flush()
        self._db_session.add(
            ResourceGrant(
                organization_id=principal.organization_id,
                subject_id=principal.subject_id,
                resource_type="webhook",
                resource_id=subscription.id,
                actions=["webhook.read", "webhook.write"],
            )
        )
        await self._db_session.flush()
        details: dict[str, object] = {
            "event_types": normalized_types,
            "status": subscription.status.value,
            "version": subscription.version,
        }
        await self._audit.record(
            self._db_session,
            principal,
            action="webhook.subscription.create",
            object_type="webhook_subscription",
            object_id=subscription.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            "webhook.subscription.created",
            "webhook_subscription",
            subscription.id,
            {"organization_id": str(principal.organization_id), **details},
        )
        return subscription

    async def list_authorized(self, principal: Principal) -> list[WebhookSubscription]:
        if principal.role is not UserRole.ADMIN:
            return []
        return list(
            (
                await self._db_session.scalars(
                    select(WebhookSubscription)
                    .join(
                        ResourceGrant,
                        (ResourceGrant.organization_id == WebhookSubscription.organization_id)
                        & (ResourceGrant.resource_type == "webhook")
                        & (ResourceGrant.resource_id == WebhookSubscription.id)
                        & (ResourceGrant.subject_id == principal.subject_id)
                        & (ResourceGrant.actions.contains(["webhook.read"])),
                    )
                    .where(WebhookSubscription.organization_id == principal.organization_id)
                    .order_by(WebhookSubscription.created_at, WebhookSubscription.id)
                )
            ).all()
        )

    async def disable(
        self,
        principal: Principal,
        subscription_id: UUID,
        *,
        expected_version: int,
    ) -> WebhookSubscription:
        subscription = await self._db_session.scalar(
            select(WebhookSubscription)
            .where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.organization_id == principal.organization_id,
            )
            .with_for_update()
        )
        if subscription is None:
            raise LookupError("webhook subscription not found")
        try:
            await AuthorizationService(self._db_session).require(
                principal,
                "webhook.write",
                ResourceRef(
                    organization_id=subscription.organization_id,
                    resource_type="webhook",
                    resource_id=subscription.id,
                    state=ResourceState.ACTIVE
                    if subscription.status is WebhookSubscriptionStatus.ACTIVE
                    else ResourceState.DISABLED,
                ),
            )
        except AuthorizationDenied as exc:
            raise LookupError("webhook subscription not found") from exc
        if subscription.version != expected_version:
            raise WebhookVersionConflict(subscription.version)
        subscription.status = WebhookSubscriptionStatus.DISABLED
        subscription.version += 1
        await self._db_session.flush()
        details = {"status": subscription.status.value, "version": subscription.version}
        await self._audit.record(
            self._db_session,
            principal,
            action="webhook.subscription.disable",
            object_type="webhook_subscription",
            object_id=subscription.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
        await self._outbox.add(
            self._db_session,
            "webhook.subscription.disabled",
            "webhook_subscription",
            subscription.id,
            {"organization_id": str(principal.organization_id), **details},
        )
        return subscription

    async def load_signing_secret(self, subscription: WebhookSubscription) -> str:
        if self._cipher is None:
            raise RuntimeError("webhook envelope encryption is not configured")
        return await self._cipher.decrypt(
            EncryptedSecret(
                ciphertext=subscription.secret_ciphertext,
                encrypted_data_key=subscription.secret_encrypted_data_key,
                nonce=subscription.secret_nonce,
                algorithm=subscription.secret_algorithm,
                key_version=subscription.secret_key_version,
            )
        )

    async def schedule(self, event: OutboxEvent) -> list[WebhookDelivery]:
        if event.event_type not in EVENT_DATA_FIELDS:
            return []
        try:
            organization_id = UUID(str(event.payload["organization_id"]))
        except (KeyError, TypeError, ValueError):
            return []
        subscriptions = list(
            (
                await self._db_session.scalars(
                    select(WebhookSubscription).where(
                        WebhookSubscription.organization_id == organization_id,
                        WebhookSubscription.status == WebhookSubscriptionStatus.ACTIVE,
                        WebhookSubscription.event_types.contains([event.event_type]),
                        WebhookSubscription.created_at <= event.occurred_at,
                    )
                )
            ).all()
        )
        deliveries: list[WebhookDelivery] = []
        for subscription in subscriptions:
            job = await JobService().enqueue(
                self._db_session,
                "webhook.deliver",
                f"webhook:{subscription.id}:{event.event_id}",
                {
                    "organization_id": str(organization_id),
                    "subscription_id": str(subscription.id),
                    "event_id": str(event.event_id),
                },
            )
            delivery_id = await self._db_session.scalar(
                insert(WebhookDelivery)
                .values(
                    organization_id=organization_id,
                    subscription_id=subscription.id,
                    event_id=event.event_id,
                    job_id=job.id,
                )
                .on_conflict_do_nothing(
                    index_elements=[WebhookDelivery.subscription_id, WebhookDelivery.event_id]
                )
                .returning(WebhookDelivery.id)
            )
            delivery = await self._db_session.scalar(
                select(WebhookDelivery).where(
                    WebhookDelivery.id == delivery_id
                    if delivery_id is not None
                    else (
                        (WebhookDelivery.subscription_id == subscription.id)
                        & (WebhookDelivery.event_id == event.event_id)
                    )
                )
            )
            if delivery is None:
                raise RuntimeError("webhook delivery was not persisted")
            deliveries.append(delivery)
        return deliveries


class WebhookVersionConflict(RuntimeError):
    def __init__(self, version: int) -> None:
        self.version = version


class WebhookDeliveryService:
    JOB_KIND = "webhook.deliver"
    LEASE_SECONDS = 60

    def __init__(
        self,
        db_session: AsyncSession,
        cipher: EnvelopeCipher,
        transport: WebhookTransport,
        *,
        worker_id: str,
    ) -> None:
        self._db_session = db_session
        self._cipher = cipher
        self._transport = transport
        self._worker_id = worker_id

    @staticmethod
    async def build(
        event: OutboxEvent,
        *,
        attempt: int,
        signing_secret: bytes,
        timestamp: int,
    ) -> WebhookRequest:
        if attempt <= 0:
            raise ValueError("delivery attempt must be positive")
        allowed_fields = EVENT_DATA_FIELDS.get(event.event_type)
        if allowed_fields is None:
            raise ValueError("event type is not allowed for webhook delivery")
        required_fields = EVENT_REQUIRED_FIELDS[event.event_type]
        if not required_fields.issubset(event.payload):
            raise ValueError("webhook event has invalid data")
        data = {key: value for key, value in event.payload.items() if key in allowed_fields}
        if any(
            not isinstance(value, (str, int, bool))
            or (isinstance(value, str) and len(value.encode("utf-8")) > 1024)
            for value in data.values()
        ):
            raise ValueError("webhook event has invalid data")
        occurred_at = event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        body: dict[str, object] = {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "event_version": event.event_version,
            "occurred_at": occurred_at,
            "delivery_attempt": attempt,
            "data": data,
        }
        body_bytes = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = WebhookSigner(signing_secret).sign(
            body=body_bytes,
            timestamp=timestamp,
        )
        return WebhookRequest(
            body=body,
            body_bytes=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
                "X-Webhook-Timestamp": str(timestamp),
            },
        )

    async def deliver(self, job_id: UUID) -> WebhookDelivery | None:
        job = await self._db_session.get(JobIntent, job_id)
        if job is None or job.kind != self.JOB_KIND:
            raise LookupError("webhook delivery job not found")
        delivery = await self._delivery_for_job(job_id)
        if delivery.state is WebhookDeliveryState.SUCCEEDED:
            return None
        claimed = await JobLeaseService(self._db_session).claim(
            job_id,
            self._worker_id,
            self.LEASE_SECONDS,
        )
        if claimed is None:
            await self._db_session.rollback()
            return None
        claim_version = claimed.version
        await self._db_session.commit()

        delivery, subscription, event = await self._context(job_id)
        if subscription.status is not WebhookSubscriptionStatus.ACTIVE:
            await JobLeaseService(self._db_session).retry(
                job_id,
                self._worker_id,
                error_code="WEBHOOK_SUBSCRIPTION_DISABLED",
                error_class=ErrorClass.NON_RETRYABLE,
                expected_version=claim_version,
            )
            delivery.state = WebhookDeliveryState.FAILED
            delivery.last_error_code = "WEBHOOK_SUBSCRIPTION_DISABLED"
            await self._db_session.commit()
            return delivery

        delivery.delivery_attempt += 1
        delivery.state = WebhookDeliveryState.DELIVERING
        delivery.last_http_status = None
        delivery.response_summary = None
        delivery.last_error_code = None
        await self._db_session.commit()

        signing_secret = (
            await self._cipher.decrypt(
                EncryptedSecret(
                    ciphertext=subscription.secret_ciphertext,
                    encrypted_data_key=subscription.secret_encrypted_data_key,
                    nonce=subscription.secret_nonce,
                    algorithm=subscription.secret_algorithm,
                    key_version=subscription.secret_key_version,
                )
            )
        ).encode("utf-8")
        try:
            await self._lock_active_job(job_id, claim_version)
            timestamp = await self._db_session.scalar(
                select(func.floor(func.extract("epoch", func.clock_timestamp())))
            )
            if timestamp is None:
                raise RuntimeError("database clock unavailable")
            request = await self.build(
                event,
                attempt=delivery.delivery_attempt,
                signing_secret=signing_secret,
                timestamp=int(timestamp),
            )
            response = await self._transport.post(
                url=subscription.endpoint_url,
                body=request.body_bytes,
                headers=request.headers,
            )
        except JobLeaseLost:
            await self._db_session.rollback()
            raise
        except Exception:
            await self._db_session.rollback()
            return await self._record_failure(
                job_id,
                claim_version,
                error_code="WEBHOOK_DELIVERY_TRANSPORT_FAILED",
                retryable=True,
            )

        if 200 <= response.status_code < 300:
            return await self._record_success(
                job_id,
                claim_version,
                status_code=response.status_code,
                response_body=response.body,
            )
        retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
        return await self._record_failure(
            job_id,
            claim_version,
            error_code=(
                "WEBHOOK_DELIVERY_RETRYABLE_RESPONSE" if retryable else "WEBHOOK_DELIVERY_REJECTED"
            ),
            retryable=retryable,
            status_code=response.status_code,
            response_body=response.body,
            retry_after_seconds=_retry_after(response.headers),
        )

    async def _record_success(
        self,
        job_id: UUID,
        claim_version: int,
        *,
        status_code: int,
        response_body: bytes,
    ) -> WebhookDelivery:
        try:
            await JobLeaseService(self._db_session).complete(
                job_id,
                self._worker_id,
                expected_version=claim_version,
            )
            delivery = await self._locked_delivery(job_id)
            delivery.state = WebhookDeliveryState.SUCCEEDED
            delivery.last_http_status = status_code
            delivery.response_summary = _safe_response_summary(response_body)
            delivery.last_error_code = None
            delivery.delivered_at = func.clock_timestamp()
            await self._db_session.commit()
            return delivery
        except JobLeaseLost:
            await self._db_session.rollback()
            raise

    async def _record_failure(
        self,
        job_id: UUID,
        claim_version: int,
        *,
        error_code: str,
        retryable: bool,
        status_code: int | None = None,
        response_body: bytes = b"",
        retry_after_seconds: int | None = None,
    ) -> WebhookDelivery:
        attempt = await self._db_session.scalar(
            select(WebhookDelivery.delivery_attempt).where(WebhookDelivery.job_id == job_id)
        )
        if attempt is None:
            raise LookupError("webhook delivery not found")
        retryable = retryable_for_attempt(requested=retryable, attempt=attempt)
        if not retryable and error_code == "WEBHOOK_DELIVERY_RETRYABLE_RESPONSE":
            error_code = "WEBHOOK_DELIVERY_ATTEMPTS_EXHAUSTED"
        elif not retryable and error_code == "WEBHOOK_DELIVERY_TRANSPORT_FAILED":
            error_code = "WEBHOOK_DELIVERY_ATTEMPTS_EXHAUSTED"
        try:
            await JobLeaseService(self._db_session).retry(
                job_id,
                self._worker_id,
                error_code=error_code,
                error_class=(ErrorClass.RETRYABLE if retryable else ErrorClass.NON_RETRYABLE),
                expected_version=claim_version,
                retry_after_seconds=retry_after_seconds,
            )
            delivery = await self._locked_delivery(job_id)
            delivery.state = (
                WebhookDeliveryState.RETRY_WAIT if retryable else WebhookDeliveryState.FAILED
            )
            delivery.last_http_status = status_code
            delivery.response_summary = (
                _safe_response_summary(response_body) if status_code is not None else None
            )
            delivery.last_error_code = error_code
            await self._db_session.commit()
            return delivery
        except JobLeaseLost:
            await self._db_session.rollback()
            raise

    async def _delivery_for_job(self, job_id: UUID) -> WebhookDelivery:
        delivery = await self._db_session.scalar(
            select(WebhookDelivery).where(WebhookDelivery.job_id == job_id)
        )
        if delivery is None:
            raise LookupError("webhook delivery not found")
        return delivery

    async def _locked_delivery(self, job_id: UUID) -> WebhookDelivery:
        delivery = await self._db_session.scalar(
            select(WebhookDelivery)
            .where(WebhookDelivery.job_id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if delivery is None:
            raise LookupError("webhook delivery not found")
        return delivery

    async def _context(
        self, job_id: UUID
    ) -> tuple[WebhookDelivery, WebhookSubscription, OutboxEvent]:
        row = (
            await self._db_session.execute(
                select(WebhookDelivery, WebhookSubscription, OutboxEvent)
                .join(
                    WebhookSubscription,
                    WebhookSubscription.id == WebhookDelivery.subscription_id,
                )
                .join(OutboxEvent, OutboxEvent.event_id == WebhookDelivery.event_id)
                .where(
                    WebhookDelivery.job_id == job_id,
                    WebhookDelivery.organization_id == WebhookSubscription.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise LookupError("webhook delivery context not found")
        return row._tuple()

    async def _lock_active_job(self, job_id: UUID, expected_version: int) -> JobIntent:
        job = await self._db_session.scalar(
            select(JobIntent)
            .where(
                JobIntent.id == job_id,
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_owner == self._worker_id,
                JobIntent.version == expected_version,
                JobIntent.lease_expires_at.is_not(None),
                JobIntent.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            raise JobLeaseLost(job_id)
        return job


def _safe_response_summary(body: bytes) -> str:
    return f"sha256={sha256(body).hexdigest()};bytes={len(body)}"


def _retry_after(headers: Mapping[str, str] | None) -> int | None:
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    return min(3600, max(0, seconds))


def retryable_for_attempt(*, requested: bool, attempt: int) -> bool:
    return requested and attempt < 5
