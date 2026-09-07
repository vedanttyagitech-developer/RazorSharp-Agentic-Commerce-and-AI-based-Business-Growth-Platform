"""Apply one verified webhook that the receiver already stored (ADR 0003 D7).

The receiver's job ends at "durably recorded and acknowledged". This handler is what
turns that record into state, and it runs asynchronously precisely so the provider gets
its 200 quickly (specification 11.3).

**The bytes are re-read from the inbox, never carried on the command.** The command
holds an inbox id and nothing else. If it carried the body, a payload could be edited
between receipt and application and the state change would be driven by bytes whose
signature was never verified.

**The row is claimed under a lock.** The row is selected ``FOR UPDATE`` and skipped
unless it is still ``RECEIVED``, so a redelivered command -- or two workers racing on one
row -- applies the event exactly once. That is belt and braces beside the receiver's
``UNIQUE (tenant_id, dedup_key)``, and it is the half that survives an operator replaying
a stored webhook from the scenario controller.

**An event for an unknown attempt is ignored, not failed.** Razorpay delivers events for
everything on the account, and an event this platform cannot correlate is not an error --
failing it would retry a delivery that will never correlate, and eventually dead-letter a
message that was simply not ours. The row is stamped ``IGNORED`` with a reason, which is
what an operator needs to see.

**Capture is applied through the kernel, monotonically.**
``payments.apply_provider_evidence`` uses ``states.monotonic_apply``, so a late
``payment.authorized`` arriving after ``payment.captured`` -- routine, since Razorpay does
not guarantee ordering -- changes nothing, and a duplicate is a no-op with
``changed=False``. This handler never decides a state itself; it builds evidence and lets
the kernel join it.

**A capture the kernel calls stale is refunded here, in the same transaction.** When the
checkout was invalidated while the payment surface was open, the kernel records the
capture and refuses the order, and says so on ``EvidenceApplied.stale_capture``. The
refund that specification 31.2 owes the buyer is admitted and its command enqueued before
this transaction commits, so a crash leaves the capture unapplied rather than applied and
unrefunded.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

import transaction_kernel as tk
from commerce_domain import sha256_hex
from durable_work import ApplyWebhookEventCommand
from payment_adapters import (
    RazorpayWebhookEvent,
    UnmappableEventError,
    parse_event,
    parse_json_body_bytes,
    payment_state_for_event,
)
from platform_db import set_tenant
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from ..settings import WorkerRuntime
from . import HandlerError, HandlerResult, reason_key
from .stale_capture import admit_stale_refund

__all__ = ["handle_apply_webhook"]

#: Razorpay payment-entity statuses and the evidence classification each supports.
#: Mirrors ``payment_adapters.razorpay.payments._PROVIDER_STATUS_CLASSIFICATION`` by
#: value. It is an allowlist in both places: a status added there and not here makes this
#: handler ignore the event rather than guess at it, which is the safe direction -- an
#: ignored event is visible in the inbox and can be replayed once the meaning is decided.
_STATUS_CLASSIFICATION: Final[Mapping[str, str]] = {
    "captured": "captured",
    "authorized": "authorized",
    "failed": "failed",
    "created": "pending",
    "pending": "pending",
    # Money was captured and then returned. The capture is a fact this attempt must
    # record; ``amount_refunded_minor`` carries the rest for the refund ledger.
    "refunded": "captured",
}

#: The evidence status each kernel payment state stands for, used only when the delivery
#: carried no payment entity and the event type is all there is to read.
_STATUS_FOR_STATE: Final[Mapping[tk.PaymentState, str]] = {
    tk.PaymentState.CAPTURED: "captured",
    tk.PaymentState.AUTHORIZED: "authorized",
    tk.PaymentState.FAILED: "failed",
    tk.PaymentState.SUBMITTED: "pending",
}

_SELECT_INBOX_FOR_UPDATE: Final = text(
    """
    SELECT id, apply_status, signature_verified, raw_body, event_type, provider_event_id
      FROM webhook_inbox
     WHERE tenant_id = :tenant AND id = :inbox
       FOR UPDATE
    """
)


def handle_apply_webhook(
    runtime: WorkerRuntime,
    command: ApplyWebhookEventCommand,
    *,
    outbox_command_id: uuid.UUID,
) -> HandlerResult:
    """Apply one stored delivery, or record exactly why it changed nothing."""
    tenant_id = uuid.UUID(command.tenant_id)
    inbox_id = uuid.UUID(command.inbox_id)
    correlation_id = uuid.UUID(command.correlation_id)

    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        row = session.execute(
            _SELECT_INBOX_FOR_UPDATE, {"tenant": tenant_id, "inbox": inbox_id}
        ).one_or_none()
        if row is None:
            raise HandlerError(
                f"webhook inbox row {inbox_id} is not visible to tenant {tenant_id}",
                code=tk.RecoveryCode.POLICY_EXCEPTION,
            )
        if row.apply_status != "RECEIVED":
            # A second delivery of the same command, or an operator replay of a row that
            # was already applied. The state change is the earlier one's; nothing here.
            return HandlerResult(
                code=tk.RecoveryCode.DUPLICATE_OPERATION,
                detail=reason_key(f"already_{row.apply_status}"),
            )
        if not row.signature_verified:
            # The receiver only stores verified deliveries, so this row should not exist.
            # It is stamped rather than raised: an unverifiable body must never move
            # money, and the evidence that one arrived is worth keeping.
            return _stamp(
                session,
                tenant_id=tenant_id,
                inbox_id=inbox_id,
                outbox_command_id=outbox_command_id,
                correlation_id=correlation_id,
                status="FAILED",
                reason="signature_not_verified",
            )

        return _apply(
            session,
            row=row,
            tenant_id=tenant_id,
            inbox_id=inbox_id,
            outbox_command_id=outbox_command_id,
            correlation_id=correlation_id,
        )


def _apply(
    session: Session,
    *,
    row: Row[Any],
    tenant_id: uuid.UUID,
    inbox_id: uuid.UUID,
    outbox_command_id: uuid.UUID,
    correlation_id: uuid.UUID,
) -> HandlerResult:
    """Parse the stored bytes, correlate, and hand the kernel one evidence record."""
    raw_body = bytes(row.raw_body)
    parsed = parse_json_body_bytes(raw_body)
    if parsed is None:
        return _stamp(
            session,
            tenant_id=tenant_id,
            inbox_id=inbox_id,
            outbox_command_id=outbox_command_id,
            correlation_id=correlation_id,
            status="IGNORED",
            reason="unparseable_body",
        )

    event = parse_event(parsed)
    attempt = _locate_attempt(session, tenant_id=tenant_id, event=event, body=parsed)
    if attempt is None:
        return _stamp(
            session,
            tenant_id=tenant_id,
            inbox_id=inbox_id,
            outbox_command_id=outbox_command_id,
            correlation_id=correlation_id,
            status="IGNORED",
            reason="attempt_not_found",
        )
    if attempt.provider_order_id is None:
        # Evidence is cross-checked against the attempt's provider order; without one
        # there is nothing to check it against, and applying it blind would let an event
        # about another order settle this attempt.
        return _stamp(
            session,
            tenant_id=tenant_id,
            inbox_id=inbox_id,
            outbox_command_id=outbox_command_id,
            correlation_id=correlation_id,
            status="IGNORED",
            reason="attempt_has_no_provider_order",
        )

    status = _evidence_status(event)
    if status is None or event.payment_id is None or event.payment_amount is None:
        # Refund speed changes, disputes, and any event whose payment entity carries no
        # amount. Recorded as seen, applied to nothing.
        return _stamp(
            session,
            tenant_id=tenant_id,
            inbox_id=inbox_id,
            outbox_command_id=outbox_command_id,
            correlation_id=correlation_id,
            status="IGNORED",
            reason=reason_key(f"no_payment_evidence.{event.event_type or 'unknown'}"),
        )

    # Built through ``from_mapping`` rather than the constructor because that is the
    # documented producer path: it normalises Razorpay's epoch-second timestamps to RFC
    # 3339 and refuses an unknown key, so a field added to the mirror on either side
    # fails here instead of being silently dropped.
    evidence = tk.ProviderEvidence.from_mapping(
        {
            "source": tk.EvidenceSource.WEBHOOK.value,
            "provider_payment_id": event.payment_id,
            "provider_order_id": event.order_id or attempt.provider_order_id,
            "amount_minor": event.payment_amount.minor,
            "currency": event.payment_amount.currency,
            "status": status,
            "provider_status": event.payment_status or event.event_type or "unknown",
            "raw_digest": sha256_hex(raw_body),
            "created_at": event.created_at,
            "amount_refunded_minor": (
                None if event.amount_refunded is None else event.amount_refunded.minor
            ),
            "event_id": row.provider_event_id,
        }
    )
    applied = tk.apply_provider_evidence(
        session,
        tenant_id=tenant_id,
        payment_attempt_id=attempt.attempt_id,
        evidence=evidence,
        correlation_id=correlation_id,
    )
    tk.record_webhook_applied(
        session,
        tenant_id=tenant_id,
        inbox_id=inbox_id,
        apply_status="APPLIED",
        apply_reason=reason_key(applied.reason),
        state_before=applied.state_before,
        state_after=applied.state_after,
        changed=applied.changed,
        outbox_command_id=outbox_command_id,
        correlation_id=correlation_id,
    )
    detail = reason_key(
        f"{'applied' if applied.changed else 'no_change'}.{applied.state_after.value}"
    )
    followups: tuple[str, ...] = ()
    if applied.stale_capture:
        # A capture on a checkout that can never be fulfilled, specification 31.2. The
        # kernel refused the order; the money still has to go back, and this is the only
        # place in the webhook path that knows it. It runs after the two calls above and
        # in their transaction: after ``apply_provider_evidence`` because that call is
        # what put the attempt in ``STALE_CAPTURE``, and after ``record_webhook_applied``
        # so the inbox row records what the *delivery* did -- the admission moves the
        # attempt on to ``REFUND_PENDING``, which no webhook ever did.
        refund_detail, followups = admit_stale_refund(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt.attempt_id,
            correlation_id=correlation_id,
            learned_from=tk.EvidenceSource.WEBHOOK.value,
            refund_reported=applied.refund_reported,
            amount_refunded_minor=evidence.amount_refunded_minor,
            source_id=inbox_id,
        )
        detail = reason_key(f"{detail}.{refund_detail}")
    return HandlerResult(code=tk.RecoveryCode.OK, detail=detail, followups=followups)


def _stamp(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    inbox_id: uuid.UUID,
    outbox_command_id: uuid.UUID,
    correlation_id: uuid.UUID,
    status: str,
    reason: str,
) -> HandlerResult:
    """Record the apply outcome on the inbox row when no state was changed."""
    tk.record_webhook_applied(
        session,
        tenant_id=tenant_id,
        inbox_id=inbox_id,
        apply_status=status,
        apply_reason=reason_key(reason),
        state_before=None,
        state_after=None,
        changed=False,
        outbox_command_id=outbox_command_id,
        correlation_id=correlation_id,
    )
    return HandlerResult(code=tk.RecoveryCode.OK, detail=reason_key(f"{status.lower()}.{reason}"))


# ------------------------------------------------------------------- correlation


def _locate_attempt(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    event: RazorpayWebhookEvent,
    body: Mapping[str, Any],
) -> tk.AttemptView | None:
    """Find the attempt this event concerns, by the most authoritative handle available.

    The provider order id is first because ``payment_attempts`` has a unique partial index
    on it, so the answer is exact. The payment id is second: not unique by constraint, and
    the kernel refuses rather than guesses if two attempts ever name one payment. The
    order's ``notes`` are last, and they are ours -- the create-order command puts the
    attempt id there -- so a delivery whose provider identifiers do not correlate can
    still be matched. Every path is tenant-scoped, so another tenant's order reads as
    absent (ADR D7).
    """
    if event.order_id:
        found = tk.find_attempt_by_provider_order(
            session, tenant_id=tenant_id, provider_order_id=event.order_id
        )
        if found is not None:
            return found
    if event.payment_id:
        found = tk.find_attempt_by_provider_payment(
            session, tenant_id=tenant_id, provider_payment_id=event.payment_id
        )
        if found is not None:
            return found
    noted = _attempt_id_from_notes(body)
    if noted is None:
        return None
    return tk.read_attempt(session, tenant_id=tenant_id, payment_attempt_id=noted)


def _attempt_id_from_notes(body: Mapping[str, Any]) -> uuid.UUID | None:
    """Read ``payment_attempt_id`` out of the order or payment notes, defensively.

    Every level is type-checked because the payload is untrusted inbound data: a wrong
    type here must produce "no attempt", never a ``TypeError`` that fails the command and
    has Razorpay redeliver it forever.
    """
    payload = body.get("payload")
    if not isinstance(payload, Mapping):
        return None
    for entity_name in ("order", "payment"):
        wrapper = payload.get(entity_name)
        if not isinstance(wrapper, Mapping):
            continue
        entity = wrapper.get("entity")
        if not isinstance(entity, Mapping):
            continue
        notes = entity.get("notes")
        if not isinstance(notes, Mapping):
            continue
        candidate = notes.get("payment_attempt_id")
        if not isinstance(candidate, str):
            continue
        try:
            return uuid.UUID(candidate)
        except ValueError:
            continue
    return None


def _evidence_status(event: RazorpayWebhookEvent) -> str | None:
    """The evidence classification this event supports, or ``None`` for "no state".

    The payment entity's own ``status`` is preferred over the event name because it is
    the provider's statement about the money at the moment of the delivery; the event
    type is the fallback for a delivery that carried no payment entity.
    ``refund.processed`` without a payment entity is left to reconciliation rather than
    guessed at -- guessing full-versus-partial is how a buyer's remaining balance gets
    stranded in a terminal ``REFUNDED``.
    """
    if event.payment_status is not None:
        return _STATUS_CLASSIFICATION.get(event.payment_status)
    try:
        state = payment_state_for_event(
            event.event_type, refund_covers_full_capture=event.refund_covers_full_capture
        )
    except UnmappableEventError:
        return None
    if state is None:
        return None
    return _STATUS_FOR_STATE.get(state)
