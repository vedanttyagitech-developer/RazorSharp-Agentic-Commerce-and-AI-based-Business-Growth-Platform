"""The automatic refund owed to a capture that landed on a checkout nobody can fulfil.

Specification 31.2 and acceptance criterion 34: a late capture against an invalidated
checkout produces exactly one refund and no fulfilment.
``payments.apply_provider_evidence`` owns the no-fulfilment half -- it writes no order,
moves the attempt to ``STALE_CAPTURE`` and raises ``EvidenceApplied.stale_capture``. It
deliberately does not refund; it hands the fact to its caller. This module is the caller
that listens.

**It lives beside the handlers rather than inside one because both need it.** A late
capture reaches the platform two ways -- Razorpay delivers ``payment.captured`` after the
invalidation, or a reconciliation round fetches the payment and learns the same thing from
provider truth -- and the buyer's money must come back either way. Neither handler may
import the other, so the shared call belongs here. It is not re-exported from
``handlers/__init__``, whose scope is the result type, the failure type and two helpers.

**The three steps commit with the evidence that caused them.** ADR 0003 requires an
admission and the command that executes it to be one transaction: a crash between them
leaves either a grant nobody will spend or a command with no authority behind it. The
caller's kernel session is that transaction. The other half of the rule -- a grant
consumed in a committed transaction before any network call -- is discharged by
``handle_refund_execute``, not here; nothing in this module talks to a provider.

**The provider's own refund ledger is part of the input.** The kernel's "remaining"
figure is computed from the platform's own ``refunds`` rows, which say nothing about money
Razorpay returned out of band -- and a capture on an invalidated checkout is exactly the
payment an operator refunds from the dashboard. Evidence that reports a refund therefore
never reaches the admission; see :func:`admit_stale_refund` for what happens instead.

**A refusal is a return value, never an exception and never a failing code.** Raising out
of the caller would roll back the applied capture and the inbox row along with the
refund, un-applying a capture Razorpay will simply redeliver into a handler that refuses
again. Returning a code outside ``OK``/``DUPLICATE_OPERATION`` would fail or bury the
outbox row, turning a declined refund into a dead-lettered webhook. So the outcome is
carried in a detail string, and the kernel's own ``refund.denied`` event is what puts the
refusal on the audit chain.
"""

from __future__ import annotations

import uuid
from typing import Final

import transaction_kernel as tk
from commerce_domain import ActorType, AdmissionDecision, CheckoutRef, RecoveryCode
from durable_work import RefundExecuteCommand, enqueue_command
from sqlalchemy.orm import Session

from . import HandlerError, reason_key

__all__ = ["admit_stale_refund"]

#: The audit aggregate a checkout's worker events hang from, as ``handlers.refund`` uses
#: it. The refund is a consequence of this checkout's invalidation, so its skip event
#: belongs on the same stream a reader is already following.
_AGGREGATE: Final = "checkout"


def admit_stale_refund(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
    learned_from: str,
    refund_reported: bool,
    amount_refunded_minor: int | None = None,
    source_id: uuid.UUID | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Admit the one refund a stale capture is owed, and enqueue the command for it.

    Call this only when ``EvidenceApplied.stale_capture`` is true, and only *after* the
    call that raised it: :func:`transaction_kernel.admit_stale_capture_refund` requires
    the attempt to be in ``STALE_CAPTURE``, and applying the evidence is what puts it
    there. Guard on ``stale_capture`` and never on ``changed`` -- the flag is also true
    for a redelivery landing on an attempt already in ``STALE_CAPTURE``, which is exactly
    the case where an earlier delivery applied the capture and no refund followed.

    Pass ``refund_reported`` and ``amount_refunded_minor`` straight from the evidence that
    produced the flag. When the provider says money has already gone back, **no automatic
    refund is admitted and a human review case is opened instead.** The kernel's remaining
    amount is computed from the platform's own ``refunds`` rows (``refunds._ledger``), and
    a dashboard refund leaves no such row -- so the admission would take the full capture
    as "remaining" and return money that is already with the buyer. A *partial* provider
    refund is not merely redundant but unanswerable here: the buyer is owed the difference,
    and :func:`transaction_kernel.admit_stale_capture_refund` refuses anything but a full
    refund (``stale_capture_requires_full_refund``). Both readings are a ledger the
    platform cannot reconcile on its own, which is what escalation is for. The attempt is
    left in ``STALE_CAPTURE`` -- :func:`transaction_kernel.escalate` does not freeze an
    attempt in which money has moved -- so the case is opened without closing the door on
    the refund a reviewer may still order through the explicit path.

    Returns the branch taken as a stable detail key, and the command types enqueued so the
    caller can put them on its :class:`~action_executor.handlers.HandlerResult`. Four
    branches. The first is the provider-refund escalation above; the remaining three are
    distinguished by the kernel's decision code rather than by ``allowed``,
    because ``AdmissionDecision`` forbids ``allowed`` beside a non-``OK`` code and a
    duplicate therefore arrives as a denial:

    * admitted -- the refund row, its grant, one ``REFUND_EXECUTE``, and the link between
      grant and command;
    * ``DUPLICATE_OPERATION`` -- an earlier delivery already admitted this refund. Nothing
      is enqueued and nothing is linked. Enqueueing anyway would send a second refund, and
      linking anyway would raise ``GrantLinkConflictError`` and roll back a webhook that
      was applied correctly;
    * any other denial -- the kernel has already written ``refund.denied``; this reports
      which one it was and lets the command finish.

    ``learned_from`` and ``source_id`` name the evidence that reached this call, so the
    skip event on a duplicate says which delivery declined to refund a second time.
    """
    if refund_reported:
        escalation = tk.escalate(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=payment_attempt_id,
            reason="stale_capture_provider_refunded",
            correlation_id=correlation_id,
        )
        if escalation.opened:
            # Only the caller that opened the case writes the detail. ``escalate`` is
            # idempotent on the case key, so a redelivery of the same news finds
            # ``opened`` false and adds nothing -- the reviewer reads one case with one
            # explanation rather than a row per delivery.
            tk.append(
                session,
                tenant=tenant_id,
                aggregate_type=_AGGREGATE,
                aggregate_id=_attempt_checkout_id(session, tenant_id, payment_attempt_id),
                event_type="worker.stale_refund_withheld",
                actor_type=ActorType.WORKER,
                principal_id=None,
                payload={
                    "payment_attempt_id": str(payment_attempt_id),
                    "case_key": escalation.case_key,
                    "amount_refunded_minor": amount_refunded_minor,
                    "learned_from": learned_from,
                    "source_id": None if source_id is None else str(source_id),
                },
                correlation_id=correlation_id,
            )
        return "stale_refund.provider_already_refunded", ()

    admission = tk.admit_stale_capture_refund(
        session,
        tenant_id=tenant_id,
        payment_attempt_id=payment_attempt_id,
        correlation_id=correlation_id,
    )
    decision = admission.decision

    if decision.code is RecoveryCode.DUPLICATE_OPERATION:
        # The kernel returns from its duplicate branch before writing any audit row, so
        # without this event the second delivery's decision *not* to refund would leave no
        # trace at all -- and "why is there only one refund for two captured webhooks" is
        # exactly the question an operator brings to the timeline.
        tk.append(
            session,
            tenant=tenant_id,
            aggregate_type=_AGGREGATE,
            aggregate_id=_checkout_of(decision).checkout_id,
            event_type="worker.stale_refund_skipped",
            actor_type=ActorType.WORKER,
            principal_id=None,
            payload={
                "payment_attempt_id": str(payment_attempt_id),
                "refund_id": None if admission.refund_id is None else str(admission.refund_id),
                "grant_id": None if admission.grant_id is None else str(admission.grant_id),
                "reason": decision.explanation,
                "learned_from": learned_from,
                "source_id": None if source_id is None else str(source_id),
            },
            correlation_id=correlation_id,
        )
        return "stale_refund.already_admitted", ()

    if not admission.allowed:
        return reason_key(f"stale_refund_denied.{decision.explanation}"), ()

    refund_id = admission.refund_id
    grant_id = admission.grant_id
    amount = admission.amount
    idem_key = admission.idem_key
    if (
        refund_id is None or grant_id is None or amount is None or idem_key is None
    ):  # pragma: no cover - the kernel's contract for an allowed admission
        raise HandlerError(
            f"stale-capture admission for attempt {payment_attempt_id} named no refund",
            code=RecoveryCode.POLICY_EXCEPTION,
        )

    # Every field is the kernel's own answer, never re-derived here. The checkout
    # reference is the locked ``checkout_versions`` row the admission read, and the
    # idempotency key is the one written on the refunds row, so a redelivered
    # ``REFUND_EXECUTE`` sends the key the provider already saw.
    checkout = _checkout_of(decision)
    command = enqueue_command(
        session,
        RefundExecuteCommand(
            tenant_id=str(tenant_id),
            refund_id=str(refund_id),
            payment_attempt_id=str(payment_attempt_id),
            grant_id=str(grant_id),
            checkout_id=str(checkout.checkout_id),
            checkout_version=checkout.version,
            content_hash=checkout.content_hash,
            amount_minor=amount.minor,
            currency=amount.currency,
            idem_key=idem_key,
            correlation_id=str(correlation_id),
        ),
        idempotency_key=idem_key,
    )
    tk.link_command(
        session,
        tenant_id=tenant_id,
        grant_id=grant_id,
        outbox_command_id=command.command_id,
        correlation_id=correlation_id,
    )
    return "stale_refund.admitted", ("REFUND_EXECUTE",)


def _attempt_checkout_id(
    session: Session, tenant_id: uuid.UUID, payment_attempt_id: uuid.UUID
) -> uuid.UUID:
    """The checkout this attempt belongs to, for an event with no kernel decision behind it.

    The provider-refund branch never calls the kernel, so it has no ``AdmissionDecision`` to
    take a checkout reference from. The attempt was locked and moved to ``STALE_CAPTURE``
    by ``apply_provider_evidence`` in this same transaction, so this read cannot miss.
    """
    attempt = tk.read_attempt(session, tenant_id=tenant_id, payment_attempt_id=payment_attempt_id)
    if attempt is None:  # pragma: no cover - the attempt was just moved in this transaction
        raise HandlerError(
            f"attempt {payment_attempt_id} vanished between applying evidence and refunding",
            code=RecoveryCode.POLICY_EXCEPTION,
        )
    return attempt.checkout_id


def _checkout_of(decision: AdmissionDecision) -> CheckoutRef:
    """The locked checkout version the kernel decided against.

    Present on every branch that reached the attempt -- an admission and a duplicate both
    carry the reference ``_lock_attempt`` read. It is absent only when no attempt was
    found, and that denial has already returned by the time this is called.
    """
    if decision.checkout is None:  # pragma: no cover - kernel contract
        raise HandlerError(
            "a stale-capture decision that reached the attempt must name its checkout",
            code=RecoveryCode.POLICY_EXCEPTION,
        )
    return decision.checkout
