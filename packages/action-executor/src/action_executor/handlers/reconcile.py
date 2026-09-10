"""Resolve an unknown outcome from provider truth, and never from a retry.

Specification 10.7 and ADR 0003 D13. An attempt reaches this handler for one of two
reasons, and both end in the same question. Either something was lost -- a create-order
response, a webhook, a browser that closed -- or the buyer's browser came *back* saying it
paid, which under ADR 0003 D8 is a claim and not evidence. Either way the platform does
not know whether money moved, and the only lawful way to find out is to ask Razorpay by
authoritative identifier and to move the attempt from what comes back. On a deployment
Razorpay cannot reach with a webhook, that second trigger is the only route to a capture.

**Reads only. No grant is consumed here and nothing is ever re-sent.** Every provider call
in this module is a ``GET``. A reconciliation that "retried" the create would be exactly
the double charge the platform exists to prevent, which is why an unknown create is
resolved by ``GET /v1/orders?receipt=...`` -- the stable receipt is what makes the lost
order findable (specification 10.6) -- and never by a second ``POST``.

**Bounded, then escalated.** Six rounds (``payments.RECONCILIATION_ATTEMPT_BOUND``) with
exponential backoff, each one recorded in ``reconciliation_runs``. The unique index on
``(tenant, attempt, reason, attempt_number)`` makes a redelivered round a no-op instead of
an extra count, so the bound is an honest number rather than an approximate one. When the
rounds run out the attempt is escalated to a person exactly once; it is never left
looping.

**Evidence is applied through the kernel.** The adapter verifies that the entity echoes
the attempt's order, amount and currency, and the kernel verifies it again and joins it
monotonically. A payment that belongs to another checkout raises rather than becoming a
state, because recording it would settle one checkout with another checkout's money.

One honest gap is recorded here rather than papered over: when the receipt lookup proves
the provider never created the order, there is no kernel edge from ``RECONCILING`` to
``FAILED``, so the attempt is not moved. The verified absence is recorded as evidence and
the bound carries it to an escalation, where a person releases it. Inventing a transition
the state machine does not declare would be a worse answer than saying so.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Final
from urllib.parse import quote

import transaction_kernel as tk
from commerce_domain import Money, RecoveryCode, sha256_hex
from durable_work import ReconcilePaymentCommand, ReconcileRefundCommand, enqueue_command
from payment_adapters import (
    EvidenceMismatchError,
    HttpRequest,
    ProviderEvidence,
    TransportError,
    build_fetch_payment_request,
    build_order_lookup_request,
    build_order_payments_request,
    fetch_order_payments,
    fetch_payment,
    find_order_by_receipt,
)
from platform_db import set_tenant
from sqlalchemy import Row, text
from sqlalchemy.orm import Session
from transaction_kernel import refunds as kernel_refunds
from transaction_kernel.payments import RECONCILIATION_ATTEMPT_BOUND

from ..faults import FaultKind, claim_fault
from ..settings import WorkerRuntime
from . import HandlerError, HandlerResult, backoff_seconds, reason_key
from .stale_capture import admit_stale_refund

__all__ = ["handle_reconcile_payment", "handle_reconcile_refund"]

#: Read operations named in ``provider_requests.operation``. Deliberately not members of
#: ``transaction_kernel.Operation``: that enum is the closed set of *mutations*, each of
#: which must name the grant it consumed, and a read consumes none.
_ORDER_LOOKUP: Final = "ORDER_LOOKUP"
_PAYMENT_FETCH: Final = "PAYMENT_FETCH"
_ORDER_PAYMENTS_FETCH: Final = "ORDER_PAYMENTS_FETCH"

#: Attempt states in which reconciliation has nothing left to establish. Reaching one of
#: these means an earlier round, a webhook or the create-order result already resolved it.
_ALREADY_RESOLVED: Final[frozenset[tk.PaymentState]] = frozenset(
    {
        tk.PaymentState.CAPTURED,
        tk.PaymentState.STALE_CAPTURE,
        tk.PaymentState.FAILED,
        tk.PaymentState.ESCALATED,
        tk.PaymentState.EXPIRED,
        tk.PaymentState.REFUNDED,
        tk.PaymentState.PARTIALLY_REFUNDED,
    }
)

#: Attempt states in which asking Razorpay can still teach the platform something.
#:
#: ``RECONCILING`` is the obvious one: an outcome was lost and the attempt was already
#: moved onto the reconciliation edge before this handler ran.
#:
#: ``SUBMITTED`` and ``AUTHORIZED`` are here because of the buyer's own return, and
#: leaving them out quietly cost the platform the second half of ADR 0003 D8. D8 says the
#: browser's report is never capture; the API keeps that promise by recording it as
#: ``BROWSER_CALLBACK`` and enqueuing this handler to go and ask the provider
#: (``payment_service.verify_client_return``). But a successful create-order leaves the
#: attempt ``SUBMITTED``, and ``record_browser_callback`` deliberately does not move it, so
#: that command arrived here on a ``SUBMITTED`` attempt, was answered
#: ``not_reconcilable.submitted``, and -- ``DUPLICATE_OPERATION`` counting as completed --
#: was marked done and dropped. The buyer had paid, Razorpay held the money, and the
#: platform never asked: no provider read, no reconciliation run, no order, no ``PAID``.
#: On a deployment with no inbound webhook route this fetch is the *only* way a payment is
#: ever captured, so the refusal to trust the browser had nothing left to defer to.
#:
#: Neither state is moved to ``RECONCILING`` on the way in. There is no declared edge for
#: that and this module does not invent transitions; none is needed, because
#: ``SUBMITTED -> CAPTURED`` and ``AUTHORIZED -> CAPTURED`` are already in the table and
#: ``apply_provider_evidence`` joins whatever comes back monotonically. Exhaustion still
#: escalates: ``escalate`` documents the hops that carry ``SUBMITTED``/``AUTHORIZED`` to
#: ``ESCALATED``. And this widens only what may be *read* -- every provider call below is
#: still a ``GET`` and still consumes no grant.
_WORTH_ASKING: Final[frozenset[tk.PaymentState]] = frozenset(
    {
        tk.PaymentState.RECONCILING,
        tk.PaymentState.SUBMITTED,
        tk.PaymentState.AUTHORIZED,
    }
)

_SELECT_REFUND: Final = text(
    """
    SELECT id, payment_attempt_id, status, amount_minor, currency, provider_refund_id
      FROM refunds
     WHERE tenant_id = :tenant AND id = :refund
    """
)


@dataclass(frozen=True, slots=True)
class _Read:
    """One provider read, in the shape ``record_provider_request`` stores it."""

    operation: str
    request: HttpRequest
    http_status: int | None
    provider_id: str | None
    outcome_code: RecoveryCode
    provider_error_code: str | None
    transport_error: str | None


@dataclass(frozen=True, slots=True)
class _Findings:
    """What one reconciliation round learned from the provider.

    ``reads`` is every request made, recorded whatever the outcome, because the evidence
    that the platform went and looked is as much a part of the proof chain as what it
    found.
    """

    reads: tuple[_Read, ...] = ()
    recovered_order_id: str | None = None
    evidence: ProviderEvidence | None = None
    code: RecoveryCode = RecoveryCode.PAYMENT_UNKNOWN
    decision: str = "no_answer"
    escalate_reason: str | None = None
    identifiers: dict[str, str | None] = field(default_factory=dict)


# ------------------------------------------------------------------ payment rounds


def handle_reconcile_payment(
    runtime: WorkerRuntime, command: ReconcilePaymentCommand
) -> HandlerResult:
    """Run one bounded reconciliation round for a payment attempt."""
    tenant_id = uuid.UUID(command.tenant_id)
    attempt_id = uuid.UUID(command.payment_attempt_id)
    correlation_id = uuid.UUID(command.correlation_id)
    reason = reason_key(command.reason)
    round_number = command.attempt_number
    buyer_status_probe = reason.startswith("buyer_status_") and reason[13:].isdigit()

    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        attempt = tk.read_attempt(session, tenant_id=tenant_id, payment_attempt_id=attempt_id)
        if attempt is None:
            raise HandlerError(
                f"command names payment attempt {attempt_id}, which this tenant cannot see",
                code=RecoveryCode.POLICY_EXCEPTION,
            )
        if attempt.status in _ALREADY_RESOLVED:
            return HandlerResult(
                code=RecoveryCode.DUPLICATE_OPERATION,
                detail=reason_key(f"already_resolved.{attempt.status.value}"),
            )
        if round_number > RECONCILIATION_ATTEMPT_BOUND:
            tk.escalate(
                session,
                tenant_id=tenant_id,
                payment_attempt_id=attempt_id,
                reason=reason_key(f"reconciliation_exhausted.{reason}"),
                correlation_id=correlation_id,
            )
            return HandlerResult(code=RecoveryCode.OK, detail="reconciliation_exhausted.escalated")
        if attempt.status in (tk.PaymentState.UNKNOWN, tk.PaymentState.REFUND_UNKNOWN):
            # The one declared edge out of an unknown outcome. Idempotent: a redelivered
            # round finds the attempt already RECONCILING and returns False.
            tk.begin_reconciling(
                session,
                tenant_id=tenant_id,
                payment_attempt_id=attempt_id,
                correlation_id=correlation_id,
            )
        elif attempt.status not in _WORTH_ASKING:
            return HandlerResult(
                code=RecoveryCode.DUPLICATE_OPERATION,
                detail=reason_key(f"not_reconcilable.{attempt.status.value}"),
            )
        snapshot = attempt

    findings = _look(runtime, snapshot, tenant_id=tenant_id)

    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        for read in findings.reads:
            _record_read(
                session,
                read,
                tenant_id=tenant_id,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
            )

        transition: str | None = None
        if findings.recovered_order_id is not None:
            tk.record_recovered_order(
                session,
                tenant_id=tenant_id,
                payment_attempt_id=attempt_id,
                provider_order_id=findings.recovered_order_id,
                correlation_id=correlation_id,
            )
            transition = "order_recovered"

        resolved = False
        stale_refund: tuple[str, tuple[str, ...]] | None = None
        if findings.evidence is not None:
            # Bound rather than passed inline: the stale-capture branch below needs the
            # provider's refunded amount off this same record.
            fetched = tk.ProviderEvidence.from_mapping(asdict(findings.evidence))
            applied = tk.apply_provider_evidence(
                session,
                tenant_id=tenant_id,
                payment_attempt_id=attempt_id,
                evidence=fetched,
                correlation_id=correlation_id,
            )
            # ``transition`` names what the *evidence* did and is not rewritten below. A
            # stale capture leaves this row reading ``RECONCILING->STALE_CAPTURE`` while
            # the committed attempt is ``REFUND_PENDING``, and that is the honest record:
            # the provider's answer was a capture, and the refund is the platform's own
            # consequence, carried on the attempt's stream and its own command.
            transition = f"{applied.state_before.value}->{applied.state_after.value}"[:64]
            # ``STALE_CAPTURE`` is in ``_ALREADY_RESOLVED``, so no further round is
            # scheduled. Correct: reconciliation has established what happened. The
            # refund it uncovered is downstream work with its own command behind it.
            resolved = applied.state_after in _ALREADY_RESOLVED
            if applied.stale_capture:
                # The same refund the webhook path admits, so the buyer is made whole
                # whichever way the news arrived (specification 31.2). A round redelivered
                # after this committed finds the attempt in ``REFUND_PENDING`` -- neither
                # already-resolved nor reconcilable -- and returns before reaching here.
                stale_refund = admit_stale_refund(
                    session,
                    tenant_id=tenant_id,
                    payment_attempt_id=attempt_id,
                    correlation_id=correlation_id,
                    learned_from=tk.EvidenceSource.PROVIDER_FETCH.value,
                    refund_reported=applied.refund_reported,
                    amount_refunded_minor=fetched.amount_refunded_minor,
                )

        followups: tuple[str, ...] = ()
        next_in: int | None = None
        decision = findings.decision
        if not resolved and findings.escalate_reason is not None:
            tk.escalate(
                session,
                tenant_id=tenant_id,
                payment_attempt_id=attempt_id,
                reason=reason_key(findings.escalate_reason),
                correlation_id=correlation_id,
            )
            decision = reason_key(f"{decision}.escalated")
        elif not resolved and buyer_status_probe:
            # A verified empty order, pending payment or temporary transport fault
            # while the buyer has Checkout open must not exhaust the financial
            # reconciliation bound. A later status probe / callback / webhook reads
            # again. No outcome or financial transition is invented here.
            decision = reason_key(f"{decision}.awaiting_provider_outcome")
        elif not resolved and round_number >= RECONCILIATION_ATTEMPT_BOUND:
            tk.escalate(
                session,
                tenant_id=tenant_id,
                payment_attempt_id=attempt_id,
                reason=reason_key(f"reconciliation_exhausted.{reason}"),
                correlation_id=correlation_id,
            )
            decision = reason_key(f"{decision}.escalated")
        elif not resolved:
            next_in = backoff_seconds(round_number, runtime.settings.reconciliation_backoff_seconds)
            enqueue_command(
                session,
                ReconcilePaymentCommand(
                    tenant_id=str(tenant_id),
                    payment_attempt_id=str(attempt_id),
                    reason=reason,
                    attempt_number=round_number + 1,
                    correlation_id=str(correlation_id),
                ),
                idempotency_key=None,
                available_in_seconds=next_in,
            )
            followups = ("RECONCILE_PAYMENT",)

        if stale_refund is not None:
            refund_detail, refund_followups = stale_refund
            decision = f"{decision}.{refund_detail}"
            followups = (*followups, *refund_followups)

        tk.record_reconciliation_run(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            attempt_number=round_number,
            reason=reason,
            identifiers_queried=findings.identifiers,
            decision=reason_key(decision),
            resulting_transition=transition,
            next_attempt_in_seconds=next_in,
            correlation_id=correlation_id,
        )

    return HandlerResult(code=RecoveryCode.OK, detail=reason_key(decision), followups=followups)


def _look(runtime: WorkerRuntime, attempt: tk.AttemptView, *, tenant_id: uuid.UUID) -> _Findings:
    """Ask the provider what happened, using the most authoritative handle available.

    With no provider order recorded, the stable receipt is the only handle there is, so
    the round starts with the lookup specification 10.6 requires before any second create.
    A found order is followed immediately by a read of its payments, because knowing the
    order exists says nothing about whether it was paid, and making that a separate round
    would add a backoff to every recovery for no new information.
    """
    if _fault_armed(runtime, tenant_id=tenant_id, attempt=attempt):
        return _Findings(
            code=RecoveryCode.PAYMENT_UNKNOWN,
            decision="fetch_unknown.scenario_fault",
            identifiers=_identifiers(attempt),
        )

    reads: list[_Read] = []
    order_id = attempt.provider_order_id
    recovered: str | None = None

    if order_id is None:
        lookup = find_order_by_receipt(
            runtime.transport, runtime.razorpay, receipt=attempt.receipt, amount=attempt.amount
        )
        reads.append(
            _Read(
                operation=_ORDER_LOOKUP,
                request=build_order_lookup_request(runtime.razorpay, receipt=attempt.receipt),
                http_status=lookup.http_status,
                provider_id=lookup.order_id,
                outcome_code=lookup.code,
                provider_error_code=lookup.provider_error_code,
                transport_error=None if lookup.http_status is not None else "TransportError",
            )
        )
        if not lookup.found or lookup.order_id is None:
            return _Findings(
                reads=tuple(reads),
                code=lookup.code,
                decision=(
                    "order_verified_absent" if lookup.verified_absent else "order_lookup_unknown"
                ),
                # A provider that affirmatively says the receipt is unused, or that
                # returns two orders for one receipt, is not going to answer differently
                # next round; the second is already HUMAN_REVIEW_REQUIRED from the adapter.
                escalate_reason=(
                    "order_lookup_conflict"
                    if lookup.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
                    else None
                ),
                identifiers=_identifiers(attempt),
            )
        order_id = recovered = lookup.order_id

    return _fetch_payments(runtime, attempt, reads=reads, order_id=order_id, recovered=recovered)


def _fetch_payments(
    runtime: WorkerRuntime,
    attempt: tk.AttemptView,
    *,
    reads: list[_Read],
    order_id: str,
    recovered: str | None,
) -> _Findings:
    """Read the payment (by id when known, else every payment on the order)."""
    identifiers = _identifiers(attempt) | {"provider_order_id": order_id}
    try:
        if attempt.provider_payment_id is not None:
            result = fetch_payment(
                runtime.transport,
                runtime.razorpay,
                attempt.provider_payment_id,
                expected_order_id=order_id,
                expected_amount=attempt.amount,
            )
            request = build_fetch_payment_request(runtime.razorpay, attempt.provider_payment_id)
            operation = _PAYMENT_FETCH
            evidence = result.evidence
            code, status = result.code, result.http_status
            provider_error = result.provider_error_code
        else:
            listed = fetch_order_payments(
                runtime.transport, runtime.razorpay, order_id, expected_amount=attempt.amount
            )
            request = build_order_payments_request(runtime.razorpay, order_id)
            operation = _ORDER_PAYMENTS_FETCH
            evidence = listed.evidence
            code, status = listed.code, listed.http_status
            provider_error = listed.provider_error_code
    except EvidenceMismatchError as exc:
        # The provider returned an entity that is not about this attempt. It may be
        # perfectly valid evidence about some other checkout, and recording it here would
        # settle this one with another's money. A person decides.
        reads.append(
            _Read(
                operation=_PAYMENT_FETCH,
                request=build_order_payments_request(runtime.razorpay, order_id),
                http_status=None,
                provider_id=order_id,
                outcome_code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
                provider_error_code=reason_key(type(exc).__name__),
                transport_error=None,
            )
        )
        return _Findings(
            reads=tuple(reads),
            recovered_order_id=recovered,
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
            decision="evidence_mismatch",
            escalate_reason="evidence_mismatch",
            identifiers=identifiers,
        )

    reads.append(
        _Read(
            operation=operation,
            request=request,
            http_status=status,
            provider_id=None if evidence is None else evidence.provider_payment_id,
            outcome_code=code,
            provider_error_code=provider_error,
            transport_error=None if status is not None else "TransportError",
        )
    )
    return _Findings(
        reads=tuple(reads),
        recovered_order_id=recovered,
        evidence=evidence,
        code=code,
        decision=(
            reason_key(f"evidence.{evidence.status}")
            if evidence is not None
            else reason_key(f"fetch_{code.value}")
        ),
        # An operator fault (refused credentials, an identifier the provider does not
        # know) will answer the same way every round; the bound would only delay it.
        escalate_reason=(
            "fetch_human_review" if code is RecoveryCode.HUMAN_REVIEW_REQUIRED else None
        ),
        identifiers=identifiers
        | {
            "provider_payment_id": (
                attempt.provider_payment_id if evidence is None else evidence.provider_payment_id
            )
        },
    )


def _identifiers(attempt: tk.AttemptView) -> dict[str, str | None]:
    """The handles this round queried by, stored on the reconciliation run as evidence."""
    return {
        "receipt": attempt.receipt,
        "provider_order_id": attempt.provider_order_id,
        "provider_payment_id": attempt.provider_payment_id,
    }


def _fault_armed(runtime: WorkerRuntime, *, tenant_id: uuid.UUID, attempt: tk.AttemptView) -> bool:
    """Consume an armed ``RECONCILE_FETCH_TIMEOUT``, so the bounded path can be shown."""
    if not runtime.settings.scenario_faults_enabled:
        return False
    with runtime.worker_session() as session:
        set_tenant(session, tenant_id)
        return (
            claim_fault(
                session,
                tenant_id=tenant_id,
                kind=FaultKind.RECONCILE_FETCH_TIMEOUT,
                checkout_id=attempt.checkout_id,
                payment_attempt_id=attempt.attempt_id,
            )
            is not None
        )


def _record_read(
    session: Session,
    read: _Read,
    *,
    tenant_id: uuid.UUID,
    attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
    refund_id: uuid.UUID | None = None,
) -> None:
    """Store one provider read. Carries no grant: a read authorises nothing."""
    tk.record_provider_request(
        session,
        tenant_id=tenant_id,
        payment_attempt_id=attempt_id,
        grant_id=None,
        refund_id=refund_id,
        operation=read.operation,
        method=read.request.method,
        # The receipt lookup carries its handle in a query string, which this column
        # refuses: it records the resource addressed, never the parameter. The receipt
        # itself is on the reconciliation run's ``identifiers_queried``.
        url=read.request.url.split("?", 1)[0],
        body_hash=sha256_hex(read.request.body),
        header_names=sorted(read.request.headers),
        http_status=read.http_status,
        provider_id=read.provider_id,
        outcome_code=read.outcome_code,
        provider_error_code=read.provider_error_code,
        response_digest=None,
        transport_error=read.transport_error,
        correlation_id=correlation_id,
    )


# ------------------------------------------------------------------- refund rounds


def handle_reconcile_refund(
    runtime: WorkerRuntime, command: ReconcileRefundCommand
) -> HandlerResult:
    """Establish whether one ``REFUND_UNKNOWN`` refund exists at the provider.

    The only evidence available today is the payment entity's ``amount_refunded``: the
    adapter has no "list this payment's refunds" call, so a refund that *does* exist
    cannot be bound to a provider refund id here. That shapes the two branches:

    * the provider's refunded total equals what the local ledger has already **settled**,
      so this refund did not land. That is the verified absence specification 10.6
      requires, and ``refunds.reconcile_refund`` marks the row ``FAILED`` so a fresh
      admission -- a new row and a **new** grant -- may issue another;
    * anything else means money may have gone back and this round cannot say which refund
      it belongs to. Nothing is concluded; the round is recorded and repeated, and the
      bound carries it to a human.

    Never issues a grant and never re-sends a refund. ``REFUND_UNKNOWN`` reconciles.
    """
    tenant_id = uuid.UUID(command.tenant_id)
    refund_id = uuid.UUID(command.refund_id)
    attempt_id = uuid.UUID(command.payment_attempt_id)
    correlation_id = uuid.UUID(command.correlation_id)
    reason = reason_key(command.reason)
    round_number = command.attempt_number

    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        refund = session.execute(
            _SELECT_REFUND, {"tenant": tenant_id, "refund": refund_id}
        ).one_or_none()
        if refund is None:
            raise HandlerError(
                f"command names refund {refund_id}, which this tenant cannot see",
                code=RecoveryCode.POLICY_EXCEPTION,
            )
        if refund.status not in (
            kernel_refunds.RefundStatus.UNKNOWN.value,
            kernel_refunds.RefundStatus.RECONCILING.value,
        ):
            return HandlerResult(
                code=RecoveryCode.DUPLICATE_OPERATION,
                detail=reason_key(f"refund_already.{refund.status}"),
            )
        attempt = tk.read_attempt(session, tenant_id=tenant_id, payment_attempt_id=attempt_id)
        if attempt is None or attempt.provider_payment_id is None:
            raise HandlerError(
                f"refund {refund_id} has no provider payment to query",
                code=RecoveryCode.POLICY_EXCEPTION,
            )
        if round_number > RECONCILIATION_ATTEMPT_BOUND:
            kernel_refunds.escalate_refund(
                session,
                tenant_id=tenant_id,
                refund_id=refund_id,
                reason_family="refund_unresolved",
                correlation_id=correlation_id,
                attempts=round_number,
            )
            return HandlerResult(code=RecoveryCode.OK, detail="refund_reconciliation.escalated")
        snapshot = attempt
        refund_amount = Money(int(refund.amount_minor), str(refund.currency))

    if refund.provider_refund_id:
        return _reconcile_known_refund(runtime, command, refund, snapshot)

    try:
        result = fetch_payment(
            runtime.transport,
            runtime.razorpay,
            snapshot.provider_payment_id or "",
            expected_order_id=snapshot.provider_order_id or "",
            expected_amount=snapshot.amount,
        )
    except EvidenceMismatchError as exc:
        # The provider answered with an entity that is not about this attempt. The payment
        # sibling has always caught this and sent it to a person; this path did not, and
        # the difference cost the buyer their refund.
        #
        # Uncaught, the exception left the handler carrying no ``.code``, so ``_code_for``
        # fell through to its default ``CONCURRENT_OPERATION`` -- which is *retryable*. The
        # outbox therefore scheduled a backoff and redelivered up to ``max_attempts``, some
        # four hours, and then buried the command. Nothing along that path escalated, no
        # reconciliation run was recorded, and not even the read was written down, so the
        # provider was asked the same question eight times with no trace that it had been
        # asked once. The refund stayed UNKNOWN and the attempt stayed REFUND_UNKNOWN, and
        # a fresh ``admit_refund`` for the same attempt is refused
        # ``RECONCILIATION_IN_PROGRESS`` -- so no route remained by which the buyer's money
        # could come back.
        #
        # Retrying was the wrong instinct twice over: mismatched evidence does not become
        # matched by asking again, and evidence about somebody else's checkout must never
        # settle this one.
        with runtime.kernel_session() as session:
            set_tenant(session, tenant_id)
            _record_read(
                session,
                _Read(
                    operation=_PAYMENT_FETCH,
                    request=build_fetch_payment_request(
                        runtime.razorpay, snapshot.provider_payment_id or ""
                    ),
                    http_status=None,
                    provider_id=snapshot.provider_payment_id,
                    outcome_code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
                    provider_error_code=reason_key(type(exc).__name__),
                    transport_error=None,
                ),
                tenant_id=tenant_id,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
                refund_id=refund_id,
            )
            kernel_refunds.escalate_refund(
                session,
                tenant_id=tenant_id,
                refund_id=refund_id,
                reason_family="evidence_mismatch",
                correlation_id=correlation_id,
                attempts=round_number,
            )
        return HandlerResult(code=RecoveryCode.OK, detail="refund_reconciliation.evidence_mismatch")

    read = _Read(
        operation=_PAYMENT_FETCH,
        request=build_fetch_payment_request(runtime.razorpay, snapshot.provider_payment_id or ""),
        http_status=result.http_status,
        provider_id=snapshot.provider_payment_id,
        outcome_code=result.code,
        provider_error_code=result.provider_error_code,
        transport_error=None if result.http_status is not None else "TransportError",
    )

    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        _record_read(
            session,
            read,
            tenant_id=tenant_id,
            attempt_id=attempt_id,
            correlation_id=correlation_id,
            refund_id=refund_id,
        )
        ledger = kernel_refunds.refundable_now(
            session, tenant_id=tenant_id, payment_attempt_id=attempt_id
        )
        decision, next_in, followups = _resolve_refund(
            session,
            runtime,
            evidence=result.evidence,
            settled_minor=ledger.settled.minor,
            tenant_id=tenant_id,
            refund_id=refund_id,
            attempt_id=attempt_id,
            correlation_id=correlation_id,
            reason=reason,
            round_number=round_number,
        )
        tk.record_reconciliation_run(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            refund_id=refund_id,
            attempt_number=round_number,
            reason=reason,
            identifiers_queried={
                "provider_payment_id": snapshot.provider_payment_id,
                "refund_amount_minor": str(refund_amount.minor),
                "provider_refund_id": refund.provider_refund_id,
            },
            decision=reason_key(decision),
            resulting_transition=None,
            next_attempt_in_seconds=next_in,
            correlation_id=correlation_id,
        )

    return HandlerResult(code=RecoveryCode.OK, detail=reason_key(decision), followups=followups)


def _resolve_refund(
    session: Session,
    runtime: WorkerRuntime,
    *,
    evidence: ProviderEvidence | None,
    settled_minor: int,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
    reason: str,
    round_number: int,
) -> tuple[str, int | None, tuple[str, ...]]:
    """Apply what the payment entity proves about this refund, or schedule another look."""
    if evidence is not None and (evidence.amount_refunded_minor or 0) <= settled_minor:
        kernel_refunds.reconcile_refund(
            session,
            tenant_id=tenant_id,
            refund_id=refund_id,
            verified="absent",
            provider_refund_id=None,
            correlation_id=correlation_id,
            attempt_number=round_number,
        )
        return "refund_verified_absent", None, ()

    if round_number >= RECONCILIATION_ATTEMPT_BOUND:
        kernel_refunds.escalate_refund(
            session,
            tenant_id=tenant_id,
            refund_id=refund_id,
            reason_family="refund_unresolved",
            correlation_id=correlation_id,
            attempts=round_number,
        )
        return "refund_unresolved.escalated", None, ()

    next_in = backoff_seconds(round_number, runtime.settings.reconciliation_backoff_seconds)
    enqueue_command(
        session,
        ReconcileRefundCommand(
            tenant_id=str(tenant_id),
            refund_id=str(refund_id),
            payment_attempt_id=str(attempt_id),
            reason=reason,
            attempt_number=round_number + 1,
            correlation_id=str(correlation_id),
        ),
        idempotency_key=None,
        available_in_seconds=next_in,
    )
    return "refund_unresolved", next_in, ("RECONCILE_REFUND",)


def _reconcile_known_refund(
    runtime: WorkerRuntime,
    command: ReconcileRefundCommand,
    refund: Row[Any],
    attempt: tk.AttemptView,
) -> HandlerResult:
    """Match the individual refund, never infer its settlement from aggregate totals."""
    tenant_id, refund_id = uuid.UUID(command.tenant_id), uuid.UUID(command.refund_id)
    attempt_id, correlation_id = (
        uuid.UUID(command.payment_attempt_id),
        uuid.UUID(command.correlation_id),
    )
    request = HttpRequest(
        method="GET",
        url=f"{runtime.razorpay.base_url}/refunds/{quote(refund.provider_refund_id, safe='')}",
        headers={"Accept": "application/json"},
        auth=(runtime.razorpay.key_id, runtime.razorpay.key_secret),
    )
    data, status, transport_error = None, None, None
    try:
        response = runtime.transport.send(request)
        status = response.status
        if response.is_success:
            data = json.loads(response.body)
    except (TransportError, ValueError) as exc:
        transport_error = type(exc).__name__
    matches = isinstance(data, dict) and (
        data.get("id") == refund.provider_refund_id
        and data.get("payment_id") == attempt.provider_payment_id
        and data.get("amount") == refund.amount_minor
        and data.get("currency") == refund.currency
        and data.get("entity") == "refund"
    )
    #: The provider's own word on this refund, read where ``data`` is known to be a mapping.
    #:
    #: ``matches`` is true only when ``data`` is a dict, so reading ``data.get("status")``
    #: behind it was already safe -- but the narrowing sat in a different variable, where
    #: neither a type checker nor a person could see it held.
    provider_status = data.get("status") if isinstance(data, dict) else None
    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        _record_read(
            session,
            _Read(
                operation="REFUND_FETCH",
                request=request,
                http_status=status,
                provider_id=refund.provider_refund_id,
                outcome_code=RecoveryCode.OK if matches else RecoveryCode.PAYMENT_UNKNOWN,
                provider_error_code=None,
                transport_error=transport_error,
            ),
            tenant_id=tenant_id,
            attempt_id=attempt_id,
            refund_id=refund_id,
            correlation_id=correlation_id,
        )
        next_in: int | None = None
        followups: tuple[str, ...] = ()
        if data is not None and not matches:
            kernel_refunds.escalate_refund(
                session,
                tenant_id=tenant_id,
                refund_id=refund_id,
                reason_family="evidence_mismatch",
                correlation_id=correlation_id,
                attempts=command.attempt_number,
            )
            decision = "refund_reconciliation.evidence_mismatch"
        elif matches and provider_status == "processed":
            kernel_refunds.reconcile_refund(
                session,
                tenant_id=tenant_id,
                refund_id=refund_id,
                verified="exists_processed",
                provider_refund_id=refund.provider_refund_id,
                correlation_id=correlation_id,
                attempt_number=command.attempt_number,
            )
            decision = "refund_verified_processed"
        else:
            if matches and provider_status == "pending":
                kernel_refunds.reconcile_refund(
                    session,
                    tenant_id=tenant_id,
                    refund_id=refund_id,
                    verified="exists_pending",
                    provider_refund_id=refund.provider_refund_id,
                    correlation_id=correlation_id,
                    attempt_number=command.attempt_number,
                )
            decision, next_in, followups = _resolve_refund(
                session,
                runtime,
                evidence=None,
                settled_minor=0,
                tenant_id=tenant_id,
                refund_id=refund_id,
                attempt_id=attempt_id,
                correlation_id=correlation_id,
                reason=command.reason,
                round_number=command.attempt_number,
            )
        tk.record_reconciliation_run(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            refund_id=refund_id,
            attempt_number=command.attempt_number,
            reason=command.reason,
            identifiers_queried={"provider_refund_id": refund.provider_refund_id},
            decision=reason_key(decision),
            resulting_transition=None,
            next_attempt_in_seconds=next_in,
            correlation_id=correlation_id,
        )
    return HandlerResult(code=RecoveryCode.OK, detail=reason_key(decision), followups=followups)
