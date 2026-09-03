from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpcore
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

SUPPORTED_EVENT_SCHEMAS = MappingProxyType(
    {
        (event_type, 1): (allowed_fields, EVENT_REQUIRED_FIELDS[event_type])
        for event_type, allowed_fields in EVENT_DATA_FIELDS.items()
    }
)

_UUID_FIELDS = frozenset(
    {
        "organization_id",
        "source_id",
        "document_id",
        "handoff_id",
        "session_id",
        "assigned_user_id",
        "message_id",
        "draft_version_id",
        "delivery_intent_id",
    }
)
_EVENT_ENUM_VALUES = MappingProxyType(
    {
        ("connector.authorized", "kind"): frozenset({"DRIVE", "GMAIL"}),
        ("connector.reauthorization_required", "kind"): frozenset({"DRIVE", "GMAIL"}),
        ("support.handoff.queued", "trigger"): frozenset(
            {
                "CUSTOMER_REQUEST",
                "LOW_CONFIDENCE",
                "REPEATED_FAILURE",
                "SENSITIVE_TOPIC",
                "SYSTEM_ERROR",
            }
        ),
        ("support.handoff.queued", "state"): frozenset({"QUEUED"}),
        ("email.draft.ready", "state"): frozenset({"AWAITING_REVIEW"}),
        ("email.delivery.sent", "state"): frozenset({"SENT"}),
        ("email.delivery.unknown", "state"): frozenset({"DELIVERY_UNKNOWN"}),
        ("retention.erasure.applied", "scope"): frozenset({"CUSTOMER", "KNOWLEDGE_DOCUMENT"}),
        ("retention.erasure.applied", "status"): frozenset({"APPLIED"}),
    }
)
_MAX_WEBHOOK_INTEGER = 2_147_483_647


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
        addresses = await _validate_delivery_endpoint(url)
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        async with httpx.AsyncClient(
            transport=_pinned_http_transport(parsed.hostname, addresses[0]),
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
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("webhook endpoint has an invalid port") from exc
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("webhook endpoint must use an ASCII URL") from exc
    if len(encoded) > 2048:
        raise ValueError("webhook endpoint is too long")
    _require_global_ip_address(parsed.hostname)
    return value


async def _resolve_endpoint_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError("webhook endpoint could not be resolved") from exc
    addresses = tuple(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise ValueError("webhook endpoint could not be resolved")
    return addresses


async def _validate_delivery_endpoint(value: str) -> tuple[str, ...]:
    """Re-resolve hostnames immediately before every external HTTP request."""

    _validated_endpoint_url(value)
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    if _ip_address_or_none(parsed.hostname) is not None:
        return (parsed.hostname,)
    addresses = await _resolve_endpoint_addresses(parsed.hostname, parsed.port or 443)
    for address in addresses:
        _require_global_ip_address(address)
    return addresses


class _PinnedAddressNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to a public address checked for this request's hostname.

    httpcore still owns the URL origin, so TLS uses the original hostname for SNI
    and certificate verification while the TCP connection cannot re-resolve it.
    """

    def __init__(
        self,
        hostname: str,
        address: str,
        *,
        delegate: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._hostname = hostname.casefold()
        self._address = address
        self._delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.casefold() != self._hostname:
            raise ValueError("webhook delivery host does not match its validated endpoint")
        return await self._delegate.connect_tcp(
            host=self._address,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,  # type: ignore[arg-type]
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._delegate.connect_unix_socket(
            path=path,
            timeout=timeout,
            socket_options=socket_options,  # type: ignore[arg-type]
        )

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


def _pinned_http_transport(hostname: str, address: str) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool._network_backend = _PinnedAddressNetworkBackend(
        hostname,
        address,
    )
    return transport


def _ip_address_or_none(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if "%" in value:
        raise ValueError("webhook endpoint must use a public IP address")
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _require_global_ip_address(value: str) -> None:
    address = _ip_address_or_none(value)
    if address is not None and (
        not address.is_global
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
    ):
        raise ValueError("webhook endpoint must use a public IP address")


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
        await _validate_delivery_endpoint(endpoint_url)
        subscription = WebhookSubscription(
            organization_id=principal.organization_id,
            created_by_id=principal.subject_id,
            endpoint_url=endpoint_url,
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
        try:
            data = validate_webhook_event(event)
        except ValueError:
            return []
        try:
            organization_id = UUID(str(data["organization_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("webhook event has invalid data") from exc
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
        data = validate_webhook_event(event)
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
        # Recovery can resume a delivery created by an older producer revision.
        # Validate its durable source before claiming the job or recording any
        # delivery side effect, so corrupt data cannot become a retryable
        # transport failure.
        delivery, subscription, event = await self._context(job_id)
        if delivery.state is WebhookDeliveryState.SUCCEEDED:
            return None
        validate_webhook_event(event)
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

        # The initial recovery validation above prevents an invalid source from
        # being claimed.  It is not enough: this long-lived session deliberately
        # uses expire_on_commit=False, and another transaction can modify the
        # Outbox row after the durable claim.  Fence the current lease first,
        # then lock and repopulate the exact linked rows.  The lock is retained
        # until the attempt is durably published so validation, signing and I/O
        # cannot be separated by a concurrent producer mutation.
        try:
            await self._lock_active_job(job_id, claim_version)
            delivery, subscription, event = await self._locked_context(job_id)
            validate_webhook_event(event)
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
            # Keep the lease and exact linked Outbox row locked through the
            # external boundary.  If this worker loses the lease, completion
            # remains fenced by the same generation below.
            response = await self._transport.post(
                url=subscription.endpoint_url,
                body=request.body_bytes,
                headers=request.headers,
            )
        except JobLeaseLost:
            await self._db_session.rollback()
            raise
        except ValueError:
            # A malformed, replaced or otherwise unverifiable persisted event
            # must never become a DELIVERING/RETRY_WAIT side effect.
            await self._db_session.rollback()
            raise
        except Exception:
            await self._db_session.rollback()
            return await self._record_failure(
                job_id,
                claim_version,
                error_code="WEBHOOK_DELIVERY_TRANSPORT_FAILED",
                retryable=True,
                attempted_delivery=True,
            )

        if 200 <= response.status_code < 300:
            return await self._record_success(
                job_id,
                claim_version,
                status_code=response.status_code,
                response_body=response.body,
            )
        retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
        # The delivery attempt and source locks above are intentionally
        # provisional until terminal publication.  Discard that provisional
        # write before recording this one durable failed attempt, otherwise the
        # current identity map would count the same HTTP call twice.
        await self._db_session.rollback()
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
            attempted_delivery=True,
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
        attempted_delivery: bool = False,
    ) -> WebhookDelivery:
        attempt = await self._db_session.scalar(
            select(WebhookDelivery.delivery_attempt).where(WebhookDelivery.job_id == job_id)
        )
        if attempt is None:
            raise LookupError("webhook delivery not found")
        if attempted_delivery:
            attempt += 1
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
            if attempted_delivery:
                delivery.delivery_attempt = attempt
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

    async def _locked_context(
        self, job_id: UUID
    ) -> tuple[WebhookDelivery, WebhookSubscription, OutboxEvent]:
        """Return the authoritative linked delivery source under row locks.

        `populate_existing` is necessary because recovery workers keep identity
        maps across the durable claim commit.  Without it, a later SELECT could
        validate a stale OutboxEvent instance despite a concurrent committed
        producer update.
        """
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
                .with_for_update()
                .execution_options(populate_existing=True)
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


def validate_webhook_event(event: OutboxEvent) -> dict[str, object]:
    if not isinstance(event.event_type, str) or event.event_type not in EVENT_DATA_FIELDS:
        raise ValueError("webhook event type is not allowed for webhook delivery")
    if (
        type(event.event_version) is not int
        or (event.event_type, event.event_version) not in SUPPORTED_EVENT_SCHEMAS
    ):
        raise ValueError("webhook event schema is not supported")
    if not isinstance(event.event_id, UUID):
        raise ValueError("webhook event has invalid data")
    if (
        not isinstance(event.occurred_at, datetime)
        or event.occurred_at.tzinfo is None
        or event.occurred_at.utcoffset() is None
    ):
        raise ValueError("webhook event has invalid timestamp")
    if not isinstance(event.payload, Mapping):
        raise ValueError("webhook event has invalid data")
    allowed_fields, required_fields = SUPPORTED_EVENT_SCHEMAS[
        (event.event_type, event.event_version)
    ]
    data = {
        key: event.payload[key]
        for key in allowed_fields
        if key in event.payload and event.payload[key] is not None
    }
    if not required_fields.issubset(data):
        raise ValueError("webhook event has invalid data")
    if any(not _valid_event_field(event.event_type, key, value) for key, value in data.items()):
        raise ValueError("webhook event has invalid data")
    return data


def _valid_event_field(event_type: str, field: str, value: object) -> bool:
    if field in _UUID_FIELDS:
        return _is_canonical_uuid(value)
    if (event_type, field) in _EVENT_ENUM_VALUES:
        return isinstance(value, str) and value in _EVENT_ENUM_VALUES[(event_type, field)]
    if field == "version":
        return type(value) is int and 1 <= value <= _MAX_WEBHOOK_INTEGER
    if field in {"handoff_boundary", "last_customer_sequence", "replay_generation", "sequence"}:
        return type(value) is int and 0 <= value <= _MAX_WEBHOOK_INTEGER
    if field == "await_customer_message":
        return type(value) is bool
    if field == "error_code":
        return (
            isinstance(value, str)
            and 0 < len(value) <= 100
            and value.isascii()
            and all(
                character.isupper() or character.isdigit() or character == "_"
                for character in value
            )
        )
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 1024


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False
