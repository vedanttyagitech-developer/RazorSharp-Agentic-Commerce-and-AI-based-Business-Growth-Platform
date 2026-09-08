"""Orders as the buyer sees them, and the buyer-confirmed refund.

An order exists only where verified capture evidence put it there. This module never
writes one; it reads what :func:`transaction_kernel.apply_provider_evidence` confirmed and
renders it beside the policy the sale was made under and every refund against it.

**The refund path is a fresh admission, never a reused authority.** Asking for money back
is a provider mutation exactly like taking it, so it goes through
:func:`transaction_kernel.admit_refund`, which issues a *new* single-use Execution Grant
bound to the *new* refunds row (ADR 0003 D10). The command that carries it is enqueued in
the same transaction and linked to the grant with
:func:`transaction_kernel.link_command`, so the proof chain runs order -> refund -> grant
-> command -> provider request without a gap.

Three consequences of that shape are worth naming, because each is a bug in the obvious
implementation:

* **The grant is per refund row, not per attempt.** Two partial refunds on one capture are
  two grants. A grant scoped to the attempt would be consumed by the first and would then
  block the second forever.
* **A denial is a 200.** ``admit_refund`` answers with a
  :class:`~commerce_domain.AdmissionDecision` for "nothing left to refund", "a refund is
  already in flight", "the outcome is still unknown". Those are the platform working, and
  ADR 0003 D15 says they are reported verbatim with the decision, not as a 4xx that tells
  every client in the chain to retry.
* **The amount is the kernel's, not the caller's.** ``amount=None`` means "everything
  still refundable" and is resolved by the kernel against its own ledger, which counts
  every refund that *may* exist at the provider. The API never subtracts anything itself.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import transaction_kernel as tk
from commerce_domain import AdmissionDecision, Money, order_reference
from durable_work.commands import RefundExecuteCommand, enqueue_command
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel.checkout_content import ContentContractError
from transaction_kernel.refunds import STALE_CAPTURE_REASON, RefundStatus

from ..deps import RequestContext
from ..errors import ProblemError
from ..schemas import MoneyOut, OrderOut, OrderState, QuoteOut, RefundOut, rfc3339
from .payment_service import AttemptRow, attempt_summary, read_attempt_row

__all__ = [
    "OrderRecord",
    "RefundRequested",
    "load_order",
    "order_payload",
    "refund_state_of",
    "request_refund",
]

_log = logging.getLogger("commerce_api.orders")

_ORDER_BY_ID = text(
    "SELECT id, checkout_id, checkout_version, payment_attempt_id, policy_receipt_hash, "
    "status, total_minor, currency, created_at FROM orders WHERE tenant_id = :t AND id = :o"
)

#: The approved version's hash *and* the document it hashes.
#:
#: Both, from one row, because an order read needs each for a different reason and a
#: second query for the second would be a second chance to read a different version.
#: ``content`` is the immutable canonical document; see :func:`order_payload` for why an
#: order renders its lines from that rather than from anything the merchant says today.
_VERSION_ROW = text(
    "SELECT content_hash, content FROM checkout_versions "
    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
)

_REFUNDS_OF_ATTEMPT = text(
    "SELECT id, status, amount_minor, currency, reason_code, provider_originated, created_at "
    "FROM refunds WHERE tenant_id = :t AND payment_attempt_id = :a "
    "ORDER BY created_at, id"
)

_REFUND_ROW = text(
    "SELECT id, status, amount_minor, currency, reason_code, provider_originated, created_at "
    "FROM refunds WHERE tenant_id = :t AND id = :r"
)

#: A refund row's own lifecycle, mapped onto the wire's ``PaymentState`` vocabulary.
#:
#: The contract in ``commerce_api.schemas.RefundOut`` (and the storefront's zod schema it
#: was written against) types a refund's ``state`` as a :class:`PaymentState`, while the
#: ``refunds`` table records a :class:`~transaction_kernel.refunds.RefundStatus`. The two
#: vocabularies describe the same lifecycle from different sides, so the mapping is total
#: and written out rather than inferred. ``PROCESSED`` is resolved separately, because
#: whether a settled refund means ``REFUNDED`` or ``PARTIALLY_REFUNDED`` depends on the
#: capture ledger and not on the row.
_REFUND_STATE: Final[dict[RefundStatus, tk.PaymentState]] = {
    RefundStatus.PENDING: tk.PaymentState.REFUND_PENDING,
    RefundStatus.FAILED: tk.PaymentState.REFUND_FAILED,
    RefundStatus.UNKNOWN: tk.PaymentState.REFUND_UNKNOWN,
    RefundStatus.RECONCILING: tk.PaymentState.RECONCILING,
    RefundStatus.ESCALATED: tk.PaymentState.ESCALATED,
    RefundStatus.PROCESSED: tk.PaymentState.REFUNDED,
}


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """One ``orders`` row plus the attempt it was confirmed from."""

    order_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    attempt: AttemptRow
    content_hash: str
    #: The canonical document ``content_hash`` names, or ``None`` when the version row it
    #: lives on could not be read. Carried on the record rather than fetched by the
    #: renderer so that the hash and the bytes it covers come from one read of one row:
    #: a renderer that fetched the document separately could hash-check a version other
    #: than the one it is about to show.
    content: Mapping[str, Any] | None
    policy_receipt_hash: str
    state: OrderState
    amount: Money
    created_at: Any


@dataclass(frozen=True, slots=True)
class RefundRequested:
    """The answer to a refund request: the kernel's decision, and the row it created."""

    decision: AdmissionDecision
    refund: RefundOut | None


def refund_state_of(status: RefundStatus, *, amount: Money, captured: Money) -> tk.PaymentState:
    """The wire state for one refund row.

    A settled refund of the whole capture is ``REFUNDED``; a settled refund of part of it
    is ``PARTIALLY_REFUNDED``. Collapsing both to ``REFUNDED`` would tell a buyer whose
    partial refund settled that their entire payment came back.
    """
    if status is RefundStatus.PROCESSED and amount < captured:
        return tk.PaymentState.PARTIALLY_REFUNDED
    return _REFUND_STATE[status]


def _refund_out(row: Any, *, captured: Money) -> RefundOut:
    amount = Money(row.amount_minor, row.currency)
    return RefundOut(
        refund_id=str(row.id),
        amount_minor=amount.minor,
        currency=amount.currency,
        state=refund_state_of(RefundStatus(row.status), amount=amount, captured=captured),
        reason=str(row.reason_code),
        # "Automatic" is the platform refunding without being asked: the stale-capture
        # path (specification 10.8) and anything Razorpay originated itself. Both are
        # counted apart from buyer-requested refunds in the retained-revenue evidence.
        automatic=row.reason_code == STALE_CAPTURE_REASON or bool(row.provider_originated),
        created_at=rfc3339(row.created_at),
    )


def load_order(session: Session, ctx: RequestContext, *, order_id: uuid.UUID) -> OrderRecord:
    """Read one order for this tenant, or refuse with 404.

    404 rather than 403 for an order belonging to another buyer, decided by the caller:
    this function refuses only what the tenant cannot see, and the route calls
    :func:`commerce_api.deps.assert_owner` on ``checkout_id`` for the ownership half.
    """
    row = session.execute(_ORDER_BY_ID, {"t": ctx.tenant_id, "o": order_id}).one_or_none()
    if row is None:
        raise ProblemError(
            404,
            "Order not found",
            "No order with that identifier belongs to this session.",
            order_id=str(order_id),
        )
    attempt = read_attempt_row(session, tenant_id=ctx.tenant_id, attempt_id=row.payment_attempt_id)
    if attempt is None:  # pragma: no cover - orders.payment_attempt_id is a foreign key
        raise ProblemError(
            500,
            "Order has no payment attempt",
            None,
            order_id=str(order_id),
        )
    version = session.execute(
        _VERSION_ROW,
        {"t": ctx.tenant_id, "c": row.checkout_id, "v": row.checkout_version},
    ).one_or_none()
    return OrderRecord(
        order_id=row.id,
        checkout_id=row.checkout_id,
        checkout_version=row.checkout_version,
        attempt=attempt,
        content_hash="" if version is None else str(version.content_hash),
        content=None if version is None else version.content,
        policy_receipt_hash=row.policy_receipt_hash,
        state=OrderState(row.status),
        amount=Money(row.total_minor, row.currency),
        created_at=row.created_at,
    )


def _quote_of(order: OrderRecord) -> QuoteOut | None:
    """The breakdown the approved document states about itself, or ``None`` if it cannot.

    Not a re-quote, and the distinction is the whole reason this is safe to render on a
    sale that has already happened. :meth:`QuoteOut.of_content` reads the immutable,
    hashed ``checkout_versions.content`` document -- the same bytes ``content_hash``
    covers and the buyer's approval binds to -- so the lines shown beneath a total are
    the lines that total is made of, whatever the merchant charges today.

    ``None`` is an anomaly here rather than the ordinary answer, which is why it is
    logged. Two things can produce it and both mean a stored row disagrees with itself:
    the version row is missing, or its document is one the kernel's own content contract
    rejects -- a shape it will not parse, or components that do not add up to the stated
    total, which :meth:`~QuoteOut.of_content` refuses to render rather than paper over.
    Neither is worth a 500 -- a buyer whose document is unreadable still has an order,
    a total and two hashes, and an order screen that will not open is a worse answer than
    one missing a table -- but neither may pass unremarked, because the last time a null
    on this field went unremarked it went unremarked for two hundred commits.
    """
    if order.content is None:
        _log.warning(
            "order %s names checkout %s version %s, which has no version row to render",
            order.order_id,
            order.checkout_id,
            order.checkout_version,
        )
        return None
    try:
        return QuoteOut.of_content(order.content, content_hash=order.content_hash)
    except ContentContractError, KeyError, ValueError:
        _log.warning(
            "order %s cannot render the document %s states it was sold under",
            order.order_id,
            order.content_hash,
            exc_info=True,
        )
        return None


def order_payload(session: Session, ctx: RequestContext, order: OrderRecord) -> OrderOut:
    """Render an order with its capture evidence, its policy receipt and its refunds.

    ``quote`` is the breakdown read back out of the approved document, via
    :func:`_quote_of`. It was ``None`` for three hundred and fifty commits, under a
    docstring claiming that rendering one "would mean re-reading a merchant catalogue that
    has moved on since the sale". That was never true. The hashed document already carried
    every figure -- ``CONTENT_KEYS`` and ``LINE_KEYS`` have not changed since the first
    migration -- and ``transaction_kernel.lines_of``, the reader, already existed and was
    already exported. What arrived later was :meth:`QuoteOut.of_content`, a convenience
    classmethod over data and a reader that were both already here.

    Recording that distinction rather than the flattering version of it: this was not a
    justification that expired, it was one that was wrong when it was written. Nothing
    caught it because a null the buyer surface handles gracefully looks like a decision,
    and no test on the order read ever asserted against it.

    ``amount_minor`` remains the authoritative total, copied onto the order at capture.
    The quote does not replace it and cannot disagree with it: the document's own total is
    what capture was checked against.
    """
    rows = session.execute(
        _REFUNDS_OF_ATTEMPT, {"t": ctx.tenant_id, "a": order.attempt.attempt_id}
    ).all()
    return OrderOut(
        order_id=str(order.order_id),
        reference=order_reference(order.order_id),
        checkout_id=str(order.checkout_id),
        version=order.checkout_version,
        content_hash=order.content_hash,
        policy_receipt_hash=order.policy_receipt_hash,
        state=order.state,
        amount_minor=order.amount.minor,
        currency=order.amount.currency,
        amount=MoneyOut.of(order.amount),
        quote=_quote_of(order),
        payment=attempt_summary(session, tenant_id=ctx.tenant_id, attempt=order.attempt),
        refunds=[_refund_out(row, captured=order.amount) for row in rows],
        created_at=rfc3339(order.created_at),
    )


def request_refund(
    session: Session,
    ctx: RequestContext,
    *,
    order: OrderRecord,
    amount_minor: int | None,
    reason: str,
) -> RefundRequested:
    """Admit one buyer-confirmed refund, and enqueue the command that will execute it.

    Runs inside the request's kernel transaction, in this order and no other:

    1. :func:`transaction_kernel.admit_refund` -- locks the attempt, checks the capture
       ledger, mints the ``refunds`` row and the single-use grant, and moves the attempt
       to ``REFUND_PENDING``. Returns a decision either way.
    2. ``REFUND_EXECUTE`` is enqueued carrying the grant id, the refund id, the checkout
       reference and the provider idempotency key the kernel chose. The worker rebuilds
       the grant binding **from this payload** and hands it to ``consume_grant``, so it
       proves it is about to send what was admitted rather than reading the grant row and
       agreeing with itself.
    3. :func:`transaction_kernel.link_command` records which command carries the grant.

    All three commit together. A crash between them leaves either no refund at all or a
    refund with the command that executes it -- never a grant nobody will spend, and
    never a command with no authority behind it.
    """
    ctx.require("refund.request")

    amount = None if amount_minor is None else Money(amount_minor, order.amount.currency)
    admission = tk.admit_refund(
        session,
        tenant_id=ctx.tenant_id,
        payment_attempt_id=order.attempt.attempt_id,
        amount=amount,
        reason_code=reason,
        principal=ctx.principal,
        correlation_id=ctx.correlation_id,
    )
    if not admission.allowed:
        return RefundRequested(decision=admission.decision, refund=None)

    # Every field below is non-None on an allowed admission; the kernel's own contract
    # says so, and asserting it here would turn a contract into a runtime coin-flip.
    refund_id = _required(admission.refund_id, "refund_id")
    grant_id = _required(admission.grant_id, "grant_id")
    refund_amount = admission.amount
    idem_key = admission.idem_key
    if refund_amount is None or idem_key is None:  # pragma: no cover - kernel contract
        raise ProblemError(500, "Incomplete refund admission", None)

    command = enqueue_command(
        session,
        RefundExecuteCommand(
            tenant_id=str(ctx.tenant_id),
            refund_id=str(refund_id),
            payment_attempt_id=str(order.attempt.attempt_id),
            grant_id=str(grant_id),
            checkout_id=str(order.checkout_id),
            checkout_version=order.checkout_version,
            content_hash=order.content_hash,
            amount_minor=refund_amount.minor,
            currency=refund_amount.currency,
            idem_key=idem_key,
            correlation_id=str(ctx.correlation_id),
        ),
        idempotency_key=idem_key,
    )
    tk.link_command(
        session,
        tenant_id=ctx.tenant_id,
        grant_id=grant_id,
        outbox_command_id=command.command_id,
        correlation_id=ctx.correlation_id,
    )

    row = session.execute(_REFUND_ROW, {"t": ctx.tenant_id, "r": refund_id}).one()
    return RefundRequested(
        decision=admission.decision,
        refund=_refund_out(row, captured=order.amount),
    )


def _required(value: uuid.UUID | None, name: str) -> uuid.UUID:
    if value is None:  # pragma: no cover - kernel contract
        raise ProblemError(500, "Incomplete refund admission", None, field=name)
    return value
