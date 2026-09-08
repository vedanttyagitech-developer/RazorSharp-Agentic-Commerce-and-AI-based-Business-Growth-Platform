"""Create the Razorpay order for one admitted payment attempt. Step 9 of the demonstration.

This is the handler the whole architecture is arranged around, and the ordering below is
the reason it exists at all.

    1. Consume the Execution Grant **and commit**.
    2. Only then call Razorpay.
    3. Record the outcome.

Each boundary is a commit, and the middle step is the only one that touches a network.
The consequence is the guarantee: if this process dies at any instant, the redelivered
command finds the grant already ``CONSUMED`` and takes the reconciliation path instead of
sending a second ``POST /v1/orders``. A worker that consumed its grant in the same
transaction as the outcome -- the tempting arrangement, since it is one transaction
instead of three -- would roll the consumption back on a crash and charge the buyer twice
on redelivery.

Three further rules hold this together:

**The binding is built from the payload, never from the grant row.**
``command.grant_binding()`` names the tenant, checkout, version, content hash, attempt,
operation and amount the worker is about to act on, and ``consume_grant`` compares it
against what the kernel admitted. A binding read back from the grant would always match
and the check would be theatre.

**An unknown outcome is never turned into a failure.** A timeout, a 5xx, an unreadable
body or a 2xx without an order id all record ``unknown``: the order may exist, so
reconciliation looks it up by the stable receipt (specification 10.6) before anything
else happens. Only a provider-confirmed refusal records ``failed``.

**An echoed amount that is not the amount we sent stops the payment.** The attempt is
recorded ``unknown`` and escalated to a person, because sending a buyer to a payment
surface for a figure they never approved is worse than any delay.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

import transaction_kernel as tk
from commerce_domain import ActorType, Money, RecoveryCode, sha256_hex
from durable_work import CreateOrderCommand, ReconcilePaymentCommand, enqueue_command
from payment_adapters import (
    CreateOrderResult,
    HttpRequest,
    build_create_order_request,
    create_order,
)
from platform_db import set_tenant
from sqlalchemy.orm import Session
from transaction_kernel.grants import GrantAlreadyConsumedError, GrantError
from transaction_kernel.payments import ProviderOrderOutcome

from ..faults import ArmedFault, FaultKind, claim_fault
from ..settings import WorkerRuntime
from . import HandlerError, HandlerResult, backoff_seconds, reason_key

__all__ = ["handle_create_order"]

#: The audit stream every payment event is written to. ``transaction_kernel.payments``
#: uses the same one, so the Money Action Proof Chain for a checkout is a single chain
#: from admission through grant consumption to the provider request and the order.
_AGGREGATE = "checkout"

#: Reconciliation always starts at round one; the bound of six is ADR D13's.
_FIRST_RECONCILIATION_ROUND = 1


@dataclass(frozen=True, slots=True)
class _ProviderCall:
    """What the create-order call established, in the shape the kernel records.

    Normalises two paths onto one: a real provider answer, and an armed scenario fault
    that replaced the call entirely. Both must produce the same evidence shape, because a
    reviewer reading ``provider_requests`` should not have to know which one happened --
    the ``transport_error`` column says so explicitly.
    """

    kind: Literal["ok", "failed", "unknown"]
    provider_order_id: str | None
    code: RecoveryCode
    reason: str
    http_status: int | None
    provider_error_code: str | None
    transport_error: str | None

    @property
    def needs_reconciliation(self) -> bool:
        return self.kind == "unknown" and self.code is not RecoveryCode.HUMAN_REVIEW_REQUIRED

    @property
    def needs_escalation(self) -> bool:
        """A provider answer that contradicts what was sent. A person must look."""
        return self.code is RecoveryCode.HUMAN_REVIEW_REQUIRED


def handle_create_order(runtime: WorkerRuntime, command: CreateOrderCommand) -> HandlerResult:
    """Execute one ``PAYMENT_CREATE_ORDER`` command exactly once, whatever the provider does."""
    tenant_id = uuid.UUID(command.tenant_id)
    attempt_id = uuid.UUID(command.payment_attempt_id)
    grant_id = uuid.UUID(command.grant_id)
    checkout_id = uuid.UUID(command.checkout_id)
    correlation_id = uuid.UUID(command.correlation_id)
    amount = Money(command.amount_minor, command.currency)

    # --- 1. consume the grant, in its own committed transaction ----------------------
    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        try:
            tk.consume_grant(session, grant_id, command.grant_binding())
        except GrantAlreadyConsumedError:
            return _resume_after_consumed_grant(
                session,
                runtime,
                tenant_id=tenant_id,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
            )
        except GrantError as exc:
            # Expired, revoked, missing, or bound to a different command. Nothing was
            # sent and nothing may be: a retry would need a fresh admission and a new
            # grant, which is the API's decision and not this worker's.
            return _refuse(
                session,
                tenant_id=tenant_id,
                checkout_id=checkout_id,
                attempt_id=attempt_id,
                grant_id=grant_id,
                correlation_id=correlation_id,
                reason=reason_key(type(exc).__name__),
            )
        tk.append(
            session,
            tenant=tenant_id,
            aggregate_type=_AGGREGATE,
            aggregate_id=checkout_id,
            event_type="worker.grant_consumed",
            actor_type=ActorType.WORKER,
            principal_id=runtime.settings.worker_id,
            payload={
                "grant_id": str(grant_id),
                "payment_attempt_id": str(attempt_id),
                "operation": tk.Operation.PAYMENT_CREATE_ORDER.value,
                "amount_minor": amount.minor,
                "currency": amount.currency,
                "receipt": command.receipt,
                "content_hash": command.content_hash,
            },
            correlation_id=correlation_id,
        )

    # --- 2. the network call, outside every transaction ------------------------------
    request = build_create_order_request(
        runtime.razorpay, amount=amount, receipt=command.receipt, notes=dict(command.notes)
    )
    call = _send(runtime, command, tenant_id=tenant_id, checkout_id=checkout_id, amount=amount)

    # --- 3. record what happened -----------------------------------------------------
    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        tk.record_provider_request(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            grant_id=grant_id,
            refund_id=None,
            operation=tk.Operation.PAYMENT_CREATE_ORDER,
            method=request.method,
            url=_resource(request),
            body_hash=sha256_hex(request.body),
            header_names=sorted(request.headers),
            http_status=call.http_status,
            provider_id=call.provider_order_id,
            outcome_code=call.code,
            provider_error_code=call.provider_error_code,
            response_digest=None,
            transport_error=call.transport_error,
            correlation_id=correlation_id,
        )
        tk.record_create_order_result(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            outcome=ProviderOrderOutcome(
                kind=call.kind,
                provider_order_id=call.provider_order_id if call.kind == "ok" else None,
                code=call.code,
                reason=call.reason,
            ),
            correlation_id=correlation_id,
        )

        followups: tuple[str, ...] = ()
        if call.needs_escalation:
            # The provider answered about a different amount, currency or receipt. The
            # attempt is UNKNOWN; freeze it rather than reconciling into the same answer.
            tk.escalate(
                session,
                tenant_id=tenant_id,
                payment_attempt_id=attempt_id,
                reason=call.reason,
                correlation_id=correlation_id,
            )
        elif call.needs_reconciliation:
            followups = _enqueue_reconciliation(
                session,
                runtime,
                tenant_id=tenant_id,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
                reason=call.reason,
            )

    return HandlerResult(code=RecoveryCode.OK, detail=call.reason, followups=followups)


# ------------------------------------------------------------------------ the send


def _send(
    runtime: WorkerRuntime,
    command: CreateOrderCommand,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    amount: Money,
) -> _ProviderCall:
    """One provider call, or one armed fault firing in its place. Never both.

    The fault is claimed and committed on the worker role before the send, so an armed
    ``CREATE_ORDER_TIMEOUT`` means no request is sent at all -- which is what keeps
    "exactly one provider request per consumed grant" true while the demonstration shows
    a lost response.
    """
    fault = _claim_create_order_fault(runtime, tenant_id=tenant_id, checkout_id=checkout_id)
    if fault is not None:
        return _ProviderCall(
            kind="unknown",
            provider_order_id=None,
            code=RecoveryCode.PAYMENT_UNKNOWN,
            reason="create_order_unknown.scenario_fault",
            http_status=None,
            provider_error_code=None,
            transport_error=fault.transport_error,
        )

    # Rebuilt inside the adapter from the same pure function that built the request
    # recorded above, so the bytes hashed into the evidence are the bytes sent.
    result = create_order(
        runtime.transport,
        runtime.razorpay,
        amount=amount,
        receipt=command.receipt,
        notes=dict(command.notes),
    )
    return _classify(result)


def _claim_create_order_fault(
    runtime: WorkerRuntime, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> ArmedFault | None:
    if not runtime.settings.scenario_faults_enabled:
        return None
    with runtime.worker_session() as session:
        set_tenant(session, tenant_id)
        return claim_fault(
            session,
            tenant_id=tenant_id,
            kind=FaultKind.CREATE_ORDER_TIMEOUT,
            checkout_id=checkout_id,
        )


def _classify(result: CreateOrderResult) -> _ProviderCall:
    """Turn the adapter's verdict into the kernel's three-way outcome.

    ``PAYMENT_FAILED`` is the only code that becomes ``failed``: the adapter reports it
    exclusively for statuses on its "definitely not performed" allowlist. Everything else
    -- including ``HUMAN_REVIEW_REQUIRED`` for an echo mismatch and for refused
    credentials -- is ``unknown``, because an order that might exist must be looked up
    before anything else is decided.
    """
    if result.code is RecoveryCode.OK and result.order_id is not None:
        return _ProviderCall(
            kind="ok",
            provider_order_id=result.order_id,
            code=result.code,
            reason="create_order_ok",
            http_status=result.http_status,
            provider_error_code=result.provider_error_code,
            transport_error=None,
        )
    if result.code is RecoveryCode.PAYMENT_FAILED:
        return _ProviderCall(
            kind="failed",
            provider_order_id=None,
            code=result.code,
            reason=reason_key(f"create_order_failed.{result.provider_error_code or 'refused'}"),
            http_status=result.http_status,
            provider_error_code=result.provider_error_code,
            transport_error=None,
        )
    return _ProviderCall(
        kind="unknown",
        provider_order_id=None,
        code=result.code,
        reason=reason_key(
            "create_order_echo_mismatch"
            if result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
            else f"create_order_unknown.{result.http_status or 'no_response'}"
        ),
        http_status=result.http_status,
        provider_error_code=result.provider_error_code,
        transport_error=None if result.http_status is not None else "TransportError",
    )


def _resource(request: HttpRequest) -> str:
    """The URL without its query string.

    ``payments.record_provider_request`` refuses a URL carrying one: that column records
    which resource was addressed, never a parameter or a credential.
    """
    return request.url.split("?", 1)[0]


# ---------------------------------------------------------------- redelivery paths


def _resume_after_consumed_grant(
    session: Session,
    runtime: WorkerRuntime,
    *,
    tenant_id: uuid.UUID,
    attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
) -> HandlerResult:
    """The grant is already spent. Read the authoritative state; never send again.

    This is the crash-recovery path and the double-delivery path at once, and the one
    thing it must never do is call the provider. What it does instead depends on how far
    the earlier delivery got:

    * a provider order is recorded -- the earlier delivery finished. Nothing to do;
    * the attempt is still ``CREATED`` -- the grant was consumed and the outcome was never
      recorded, so the request may or may not have been sent. That is the definition of
      ``unknown``: record it and reconcile by receipt;
    * the attempt is already ``UNKNOWN`` -- the outcome was recorded but the follow-up may
      not have been enqueued. Enqueue it; a duplicate reconciliation is a no-op because
      the run's unique index collapses it;
    * anything else -- another path already owns this attempt.
    """
    attempt = tk.read_attempt(session, tenant_id=tenant_id, payment_attempt_id=attempt_id)
    if attempt is None:
        raise HandlerError(
            f"command names payment attempt {attempt_id}, which this tenant cannot see",
            code=RecoveryCode.POLICY_EXCEPTION,
        )
    if attempt.provider_order_id is not None:
        return HandlerResult(code=RecoveryCode.DUPLICATE_OPERATION, detail="order_already_created")

    if attempt.status is tk.PaymentState.CREATED:
        tk.record_create_order_result(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            outcome=ProviderOrderOutcome(
                kind="unknown",
                provider_order_id=None,
                code=RecoveryCode.PAYMENT_UNKNOWN,
                reason="create_order_unknown.outcome_unrecorded",
            ),
            correlation_id=correlation_id,
        )
    elif attempt.status is not tk.PaymentState.UNKNOWN:
        return HandlerResult(
            code=RecoveryCode.DUPLICATE_OPERATION,
            detail=reason_key(f"attempt_already.{attempt.status.value}"),
        )

    followups = _enqueue_reconciliation(
        session,
        runtime,
        tenant_id=tenant_id,
        attempt_id=attempt_id,
        correlation_id=correlation_id,
        reason="create_order_unknown.grant_already_consumed",
    )
    return HandlerResult(code=RecoveryCode.OK, detail="grant_already_consumed", followups=followups)


def _refuse(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    attempt_id: uuid.UUID,
    grant_id: uuid.UUID,
    correlation_id: uuid.UUID,
    reason: str,
) -> HandlerResult:
    """Record that a grant would not authorise this command, and stop.

    Returned rather than raised so the audit row commits: an unusable grant is evidence,
    and raising would roll back the only record that the worker ever looked at it. The
    outbox buries the command on this code, which is what puts it in front of a person.
    """
    tk.append(
        session,
        tenant=tenant_id,
        aggregate_type=_AGGREGATE,
        aggregate_id=checkout_id,
        event_type="worker.grant_refused",
        actor_type=ActorType.WORKER,
        principal_id=None,
        payload={
            "grant_id": str(grant_id),
            "payment_attempt_id": str(attempt_id),
            "operation": tk.Operation.PAYMENT_CREATE_ORDER.value,
            "reason": reason,
        },
        correlation_id=correlation_id,
    )
    return HandlerResult(code=RecoveryCode.AUTHORITY_INSUFFICIENT, detail=reason)


def _enqueue_reconciliation(
    session: Session,
    runtime: WorkerRuntime,
    *,
    tenant_id: uuid.UUID,
    attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
    reason: str,
) -> tuple[str, ...]:
    """Enqueue round one of the bounded reconciliation, in the caller's transaction."""
    enqueue_command(
        session,
        ReconcilePaymentCommand(
            tenant_id=str(tenant_id),
            payment_attempt_id=str(attempt_id),
            reason=reason_key(reason),
            attempt_number=_FIRST_RECONCILIATION_ROUND,
            correlation_id=str(correlation_id),
        ),
        idempotency_key=None,
        available_in_seconds=backoff_seconds(
            _FIRST_RECONCILIATION_ROUND, runtime.settings.reconciliation_backoff_seconds
        ),
    )
    return ("RECONCILE_PAYMENT",)
