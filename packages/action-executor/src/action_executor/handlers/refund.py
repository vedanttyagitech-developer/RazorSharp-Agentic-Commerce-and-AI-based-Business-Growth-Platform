"""Execute one admitted refund. The path where a mistake pays a buyer twice.

Specification 10.6 and 11.4. The ordering is the create-order handler's, for the same
reason: consume the ``REFUND_EXECUTE`` grant in a committed transaction, then send, then
record. A redelivery finds the grant consumed and reconciles instead of refunding again.

Two rules are specific to refunds and neither is negotiable.

**The idempotency key is the kernel's, carried on the command.** ``admit_refund`` derives
it once and stores it on the ``refunds`` row; the command carries that exact value and it
goes out as ``X-Refund-Idempotency``. A key derived here would be a *second*
derivation of the same intent, and two derivations that ever disagree -- a different
ordinal after a restart, say -- create a second refund at the provider for one refund
locally.

**A timeout is ``REFUND_UNKNOWN``, and ``REFUND_UNKNOWN`` never retries.** The two refund
failure states are not interchangeable: ``REFUND_FAILED`` is a provider-confirmed absence
and may be re-admitted under a fresh grant, while ``REFUND_UNKNOWN`` means a refund may
already exist and only reconciliation may say. This handler records the unknown and
enqueues the reconciliation; it has no branch that sends again.
"""

from __future__ import annotations

import uuid
from typing import Final

import transaction_kernel as tk
from commerce_domain import ActorType, Money, RecoveryCode, sha256_hex
from durable_work import ReconcileRefundCommand, RefundExecuteCommand, enqueue_command
from payment_adapters import (
    RefundDecision,
    RefundPlan,
    RefundRefusal,
    RefundResult,
    build_refund_request,
    execute_refund,
)
from platform_db import set_tenant
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel.grants import GrantAlreadyConsumedError, GrantError
from transaction_kernel.refunds import RefundOutcome, RefundStatus

from ..faults import FaultKind, claim_fault
from ..settings import WorkerRuntime
from . import HandlerError, HandlerResult, backoff_seconds, reason_key

__all__ = ["handle_refund_execute"]

_AGGREGATE: Final = "checkout"
_FIRST_RECONCILIATION_ROUND: Final = 1

#: What each provider verdict means to ``refunds.record_refund_result``. Both settled
#: postures collapse to ``processed``: whether the *attempt* becomes ``REFUNDED`` or
#: ``PARTIALLY_REFUNDED`` is a fact about the ledger, and the kernel reads the ledger.
_OUTCOME_FOR_STATE: Final[dict[tk.PaymentState, RefundOutcome]] = {
    tk.PaymentState.REFUNDED: "processed",
    tk.PaymentState.PARTIALLY_REFUNDED: "processed",
    tk.PaymentState.REFUND_FAILED: "failed",
    tk.PaymentState.REFUND_UNKNOWN: "unknown",
    tk.PaymentState.REFUND_PENDING: "pending",
}

_SELECT_REFUND: Final = text(
    """
    SELECT id, status, amount_minor, currency, idem_key, provider_refund_id
      FROM refunds
     WHERE tenant_id = :tenant AND id = :refund
    """
)


def handle_refund_execute(runtime: WorkerRuntime, command: RefundExecuteCommand) -> HandlerResult:
    """Send one admitted refund exactly once, and record what came back."""
    tenant_id = uuid.UUID(command.tenant_id)
    refund_id = uuid.UUID(command.refund_id)
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
                refund_id=refund_id,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
            )
        except GrantError as exc:
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
                    "refund_id": str(refund_id),
                    "operation": tk.Operation.REFUND_EXECUTE.value,
                    "reason": reason_key(type(exc).__name__),
                },
                correlation_id=correlation_id,
            )
            return HandlerResult(
                code=RecoveryCode.AUTHORITY_INSUFFICIENT,
                detail=reason_key(type(exc).__name__),
            )

        attempt = tk.read_attempt(session, tenant_id=tenant_id, payment_attempt_id=attempt_id)
        if attempt is None or attempt.provider_payment_id is None:
            # Unreachable through admission, which only admits a refund against a capture.
            # Raised rather than sent, because a refund needs a payment to refund and
            # inventing one is not an option.
            raise HandlerError(
                f"refund {refund_id} names attempt {attempt_id}, which has no provider payment",
                code=RecoveryCode.POLICY_EXCEPTION,
            )
        payment_id = attempt.provider_payment_id
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
                "refund_id": str(refund_id),
                "payment_attempt_id": str(attempt_id),
                "operation": tk.Operation.REFUND_EXECUTE.value,
                "amount_minor": amount.minor,
                "currency": amount.currency,
            },
            correlation_id=correlation_id,
        )

    # --- 2. the network call ---------------------------------------------------------
    plan = _plan(command, payment_id=payment_id, amount=amount)
    decision = RefundDecision(
        allowed=True,
        code=RecoveryCode.REFUND_ALLOWED,
        explanation=RefundRefusal.OK,
        plan=plan,
    )
    request = build_refund_request(runtime.razorpay, plan)
    result, transport_error = _send(
        runtime, decision, tenant_id=tenant_id, checkout_id=checkout_id, attempt_id=attempt_id
    )

    # --- 3. record what happened -----------------------------------------------------
    outcome: RefundOutcome = _OUTCOME_FOR_STATE.get(result.payment_state, "unknown")
    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        tk.record_provider_request(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            grant_id=grant_id,
            refund_id=refund_id,
            operation=tk.Operation.REFUND_EXECUTE,
            method=request.method,
            url=request.url.split("?", 1)[0],
            body_hash=sha256_hex(request.body),
            header_names=sorted(request.headers),
            http_status=result.http_status,
            provider_id=result.refund_id,
            outcome_code=result.code,
            provider_error_code=result.provider_error_code,
            response_digest=None,
            transport_error=transport_error,
            correlation_id=correlation_id,
        )
        tk.record_refund_result(
            session,
            tenant_id=tenant_id,
            refund_id=refund_id,
            outcome=outcome,
            provider_refund_id=result.refund_id,
            correlation_id=correlation_id,
        )
        followups: tuple[str, ...] = ()
        if outcome == "unknown":
            followups = _enqueue_reconciliation(
                session,
                runtime,
                tenant_id=tenant_id,
                refund_id=refund_id,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
                reason="refund_unknown",
            )

    return HandlerResult(
        code=RecoveryCode.OK, detail=reason_key(f"refund_{outcome}"), followups=followups
    )


# ------------------------------------------------------------------------ the send


def _plan(command: RefundExecuteCommand, *, payment_id: str, amount: Money) -> RefundPlan:
    """Wrap the admitted refund in the adapter's plan shape without re-deciding anything.

    ``plan_refund`` exists for callers that have to decide whether a refund is allowed.
    This worker holds a refund the kernel already admitted, with a grant it has already
    consumed, so re-running that decision here would be a second opinion on a settled
    question -- and a second derivation of the idempotency key, which is the one value
    that must not be derived twice.

    ``sequence`` is zero deliberately: it is an input to the adapter's own key derivation,
    which is bypassed here, and a plausible-looking ordinal would suggest this key came
    from somewhere it did not. ``resulting_state`` is likewise not consulted -- the kernel
    decides ``REFUNDED`` versus ``PARTIALLY_REFUNDED`` from the committed ledger.
    """
    return RefundPlan(
        payment_id=payment_id,
        amount=amount,
        idempotency_key=command.idem_key,
        sequence=0,
        is_full_remaining=False,
        resulting_state=tk.PaymentState.PARTIALLY_REFUNDED,
    )


def _send(
    runtime: WorkerRuntime,
    decision: RefundDecision,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    attempt_id: uuid.UUID,
) -> tuple[RefundResult, str | None]:
    """One refund request, or one armed fault firing in its place. Never both."""
    fault = None
    if runtime.settings.scenario_faults_enabled:
        with runtime.worker_session() as session:
            set_tenant(session, tenant_id)
            fault = claim_fault(
                session,
                tenant_id=tenant_id,
                kind=FaultKind.REFUND_TIMEOUT,
                checkout_id=checkout_id,
                payment_attempt_id=attempt_id,
            )
    if fault is not None:
        return (
            RefundResult(
                code=RecoveryCode.PAYMENT_UNKNOWN,
                payment_state=tk.PaymentState.REFUND_UNKNOWN,
                refund_id=None,
                http_status=None,
            ),
            fault.transport_error,
        )

    result = execute_refund(runtime.transport, runtime.razorpay, decision)
    return result, (None if result.http_status is not None else "TransportError")


# ---------------------------------------------------------------- redelivery path


def _resume_after_consumed_grant(
    session: Session,
    runtime: WorkerRuntime,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
) -> HandlerResult:
    """The grant is spent. Reconcile; never send a second refund.

    A ``PENDING`` row means the earlier delivery consumed its grant and never recorded an
    outcome, so the provider may or may not hold a refund. That is the definition of
    ``REFUND_UNKNOWN``, and it is recorded as such rather than re-sent -- re-sending is
    how a buyer is paid back twice, and the stored idempotency key protects against it
    only while the provider still remembers the key.
    """
    row = session.execute(_SELECT_REFUND, {"tenant": tenant_id, "refund": refund_id}).one_or_none()
    if row is None:
        raise HandlerError(
            f"command names refund {refund_id}, which this tenant cannot see",
            code=RecoveryCode.POLICY_EXCEPTION,
        )
    if row.status == RefundStatus.PENDING.value:
        tk.record_refund_result(
            session,
            tenant_id=tenant_id,
            refund_id=refund_id,
            outcome="unknown",
            provider_refund_id=None,
            correlation_id=correlation_id,
        )
    elif row.status != RefundStatus.UNKNOWN.value:
        return HandlerResult(
            code=RecoveryCode.DUPLICATE_OPERATION,
            detail=reason_key(f"refund_already.{row.status}"),
        )

    followups = _enqueue_reconciliation(
        session,
        runtime,
        tenant_id=tenant_id,
        refund_id=refund_id,
        attempt_id=attempt_id,
        correlation_id=correlation_id,
        reason="refund_unknown.grant_already_consumed",
    )
    return HandlerResult(code=RecoveryCode.OK, detail="grant_already_consumed", followups=followups)


def _enqueue_reconciliation(
    session: Session,
    runtime: WorkerRuntime,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
    reason: str,
) -> tuple[str, ...]:
    """Enqueue round one of the bounded refund reconciliation, in this transaction."""
    enqueue_command(
        session,
        ReconcileRefundCommand(
            tenant_id=str(tenant_id),
            refund_id=str(refund_id),
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
    return ("RECONCILE_REFUND",)
