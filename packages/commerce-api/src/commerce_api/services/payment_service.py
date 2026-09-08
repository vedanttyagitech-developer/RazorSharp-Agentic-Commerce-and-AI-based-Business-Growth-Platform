"""The payment handoff and the client return (ADR 0003 D8).

Two operations live here, and the second one is the rule this whole architecture exists
to make unbreakable.

**The handoff** answers "what does the browser need to open Razorpay Checkout": the
provider order id the worker created, the *public* key id, the amount and the currency.
It is a read, and it never mints anything -- if the worker has not created the order yet,
``razorpay_order_id`` is ``null`` and the surface waits.

**The client return** is where a naive implementation loses money. Razorpay's Checkout
hands the browser ``razorpay_order_id``, ``razorpay_payment_id`` and
``razorpay_signature``, and the obvious thing to do is verify the signature and mark the
payment captured. That is wrong, and the reason is worth stating plainly: a valid
signature proves only that whoever produced it holds the API key secret. It does not
prove money moved. The browser is not a party to the settlement; it is a bystander that
was told something. Treating its report as capture evidence means a checkout can be
marked paid by anything that can replay a callback URL, and it means a genuine buyer
whose browser closed early looks unpaid while their money is gone.

So ADR 0003 D8 fixes the rule: this endpoint

* confirms the session owns the attempt the callback names,
* verifies ``HMAC(order_id|payment_id)`` in constant time,
* records the payment id as ``BROWSER_CALLBACK`` evidence through
  :func:`transaction_kernel.record_browser_callback`, which refuses to overwrite a
  payment id it already holds,
* enqueues ``RECONCILE_PAYMENT`` so the worker asks Razorpay what actually happened,
* and **changes no payment state and creates no order**.

Capture is applied only by :func:`transaction_kernel.apply_provider_evidence` from
``WEBHOOK`` or ``PROVIDER_FETCH`` evidence, through ``states.monotonic_apply`` so
``CAPTURED`` never regresses. The test that proves this endpoint is correct is the one
that asserts the payment state is unchanged and no ``orders`` row exists after a
*successful* verification.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

import transaction_kernel as tk
from commerce_domain import Money, RecoveryCode
from durable_work.commands import ReconcilePaymentCommand, enqueue_command
from payment_adapters import verify_payment_signature
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import RequestContext, assert_owner
from ..errors import ProblemError
from ..schemas import AttemptOut, CaptureEvidenceOut, uuid_str
from ..settings import Settings

__all__ = [
    "BROWSER_CALLBACK_MESSAGE",
    "AttemptRow",
    "PaymentHandoff",
    "VerifyOutcome",
    "attempt_summary",
    "build_handoff",
    "latest_attempt",
    "order_row_for_attempt",
    "read_attempt_row",
    "verify_client_return",
]

#: What the buyer surface shows after a client return. Deliberately not "payment
#: successful": the platform does not know that yet, and saying so would make the one
#: honest thing about this endpoint into a lie.
BROWSER_CALLBACK_MESSAGE: Final[str] = (
    "Your return from Razorpay was verified and recorded. Confirmation waits on the "
    "provider's own report -- a browser callback is never treated as proof of payment."
)

_ATTEMPT_COLUMNS: Final[str] = (
    "id, checkout_id, checkout_version, status, amount_minor, currency, receipt, "
    "provider_order_id, provider_payment_id"
)

_LATEST_ATTEMPT = text(
    f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempts "  # noqa: S608 - literal above
    "WHERE tenant_id = :t AND checkout_id = :c ORDER BY created_at DESC, id DESC LIMIT 1"
)

_ATTEMPT_BY_ID = text(
    f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempts "  # noqa: S608 - literal above
    "WHERE tenant_id = :t AND id = :a"
)

_PAYMENT_GRANT = text(
    "SELECT id FROM execution_grants WHERE tenant_id = :t AND payment_attempt_id = :a "
    "AND operation = 'PAYMENT_CREATE_ORDER' ORDER BY issued_at DESC, id DESC LIMIT 1"
)

#: Payment reconciliation only: a refund's rounds carry ``refund_id`` and are counted
#: against the refund, not against the attempt the buyer is watching.
_RECONCILIATION_COUNT = text(
    "SELECT count(*) FROM reconciliation_runs WHERE tenant_id = :t "
    "AND payment_attempt_id = :a AND refund_id IS NULL"
)

_ORDER_EVIDENCE = text(
    "SELECT id, capture_evidence FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a"
)

_VERSION_ROW = text(
    "SELECT content_hash, total_minor, currency, status FROM checkout_versions "
    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
)

_MERCHANT_NAME = text("SELECT name FROM merchants WHERE tenant_id = :t AND id = :m")


@dataclass(frozen=True, slots=True)
class AttemptRow:
    """One ``payment_attempts`` row, detached from the transaction that read it.

    Deliberately not :class:`transaction_kernel.payments.AttemptView`: this is reached
    from a *checkout*, which the kernel's reads do not offer, and it is read on the app
    role where no lock is taken. Where an attempt id is already known the kernel's
    :func:`transaction_kernel.read_attempt` is the right call and this type is not used.
    """

    attempt_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    state: tk.PaymentState
    amount: Money
    receipt: str
    provider_order_id: str | None
    provider_payment_id: str | None


@dataclass(frozen=True, slots=True)
class PaymentHandoff:
    """Everything the browser needs to open Razorpay Checkout, and nothing more.

    ``razorpay_key_id`` is the *public* key id. It is published on purpose: Razorpay's
    Checkout script needs it in the page, and it authorises nothing on its own. The key
    secret and the webhook secret never leave this process, which is why the signature
    this handoff's callback carries can only have been produced by Razorpay.
    """

    checkout_id: uuid.UUID
    version: int
    attempt_id: uuid.UUID | None
    state: tk.PaymentState | None
    razorpay_key_id: str
    razorpay_order_id: str | None
    amount: Money
    merchant_name: str
    description: str


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """What a client return did. ``accepted`` is the kernel's verdict, not the signature's.

    A verified signature whose payment id contradicts one already recorded is
    ``accepted=False``: the first recorded id is what reconciliation fetches, and letting
    a second browser message replace it is how the platform ends up asking the provider
    about the wrong payment.
    """

    accepted: bool
    attempt_id: uuid.UUID
    state: tk.PaymentState
    code: RecoveryCode
    reason: str
    enqueued_reconciliation: bool


# --------------------------------------------------------------------------- reads


def _row_to_attempt(row: Any) -> AttemptRow:
    return AttemptRow(
        attempt_id=row.id,
        checkout_id=row.checkout_id,
        checkout_version=row.checkout_version,
        state=tk.PaymentState(row.status),
        amount=Money(row.amount_minor, row.currency),
        receipt=row.receipt,
        provider_order_id=row.provider_order_id,
        provider_payment_id=row.provider_payment_id,
    )


def latest_attempt(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> AttemptRow | None:
    """The most recent attempt on a checkout, or ``None``.

    Most recent rather than "the live one": a checkout has at most one non-terminal
    attempt (a partial unique index enforces it), but a failed attempt followed by a
    fresh admission leaves two, and the buyer is looking at the newer.
    """
    row = session.execute(_LATEST_ATTEMPT, {"t": tenant_id, "c": checkout_id}).one_or_none()
    return None if row is None else _row_to_attempt(row)


def read_attempt_row(
    session: Session, *, tenant_id: uuid.UUID, attempt_id: uuid.UUID
) -> AttemptRow | None:
    """One attempt by id, on the app role. No lock; for read endpoints only."""
    row = session.execute(_ATTEMPT_BY_ID, {"t": tenant_id, "a": attempt_id}).one_or_none()
    return None if row is None else _row_to_attempt(row)


def _capture_evidence(evidence: Any) -> CaptureEvidenceOut | None:
    """Render ``orders.capture_evidence`` for the wire.

    Returns ``None`` for a browser callback even though one can never reach this column,
    because a defensive filter here is cheaper than the alternative discovery: an order
    surface that showed ``BROWSER_CALLBACK`` as the reason a sale was confirmed would be
    telling a reviewer the exact opposite of what D8 guarantees.
    """
    if not isinstance(evidence, dict):
        return None
    kind = evidence.get("source")
    if kind not in (tk.EvidenceSource.WEBHOOK.value, tk.EvidenceSource.PROVIDER_FETCH.value):
        return None
    reference = evidence.get("provider_payment_id") or evidence.get("provider_order_id") or ""
    verified_at = evidence.get("captured_at") or evidence.get("created_at") or ""
    return CaptureEvidenceOut(
        kind=str(kind), reference=str(reference), verified_at=str(verified_at)
    )


def attempt_summary(session: Session, *, tenant_id: uuid.UUID, attempt: AttemptRow) -> AttemptOut:
    """The wire summary of one attempt, with its grant, its evidence and its round count.

    Four small reads rather than one join, because three of them are usually misses and a
    join would make the common case -- an attempt with no order and no reconciliation --
    pay for the rare one.
    """
    grant_id = session.execute(
        _PAYMENT_GRANT, {"t": tenant_id, "a": attempt.attempt_id}
    ).scalar_one_or_none()
    rounds = session.execute(
        _RECONCILIATION_COUNT, {"t": tenant_id, "a": attempt.attempt_id}
    ).scalar_one()
    order_row = session.execute(
        _ORDER_EVIDENCE, {"t": tenant_id, "a": attempt.attempt_id}
    ).one_or_none()
    return AttemptOut(
        attempt_id=str(attempt.attempt_id),
        version=attempt.checkout_version,
        state=attempt.state,
        razorpay_order_id=attempt.provider_order_id,
        razorpay_payment_id=attempt.provider_payment_id,
        grant_id=uuid_str(grant_id),
        capture_evidence=None
        if order_row is None
        else _capture_evidence(order_row.capture_evidence),
        reconciliation_attempts=int(rounds),
    )


def build_handoff(
    session: Session,
    ctx: RequestContext,
    *,
    checkout_id: uuid.UUID,
    merchant_id: uuid.UUID,
    version: int,
    settings: Settings,
) -> PaymentHandoff:
    """Assemble the Razorpay Checkout handoff for a checkout this session owns.

    The amount comes from the *attempt* when one exists and from the checkout version
    otherwise. That ordering matters: the attempt's amount is the figure the Execution
    Grant was issued for and the figure Razorpay's order carries, so once an attempt
    exists it is the only amount the browser may be shown. Reading the version instead
    would let a merchant-side price change after admission put a different number in
    front of the buyer than the one that will actually be charged.
    """
    attempt = latest_attempt(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    version_row = session.execute(
        _VERSION_ROW, {"t": ctx.tenant_id, "c": checkout_id, "v": version}
    ).one_or_none()
    if attempt is not None:
        amount = attempt.amount
    elif version_row is not None:
        amount = Money(version_row.total_minor, version_row.currency)
    else:
        raise ProblemError(
            404,
            "Checkout version not found",
            "This checkout has no version to pay for yet.",
            checkout_id=str(checkout_id),
            version=version,
        )

    merchant_name = session.execute(
        _MERCHANT_NAME, {"t": ctx.tenant_id, "m": merchant_id}
    ).scalar_one_or_none()
    razorpay = settings.razorpay()
    return PaymentHandoff(
        checkout_id=checkout_id,
        version=attempt.checkout_version if attempt is not None else version,
        attempt_id=None if attempt is None else attempt.attempt_id,
        state=None if attempt is None else attempt.state,
        razorpay_key_id=razorpay.key_id,
        razorpay_order_id=None if attempt is None else attempt.provider_order_id,
        amount=amount,
        merchant_name=str(merchant_name or "Merchant"),
        description=f"Order {str(checkout_id)[:8]} - {merchant_name or 'Merchant'}",
    )


# ------------------------------------------------------------------- client return


def verify_client_return(
    session: Session,
    ctx: RequestContext,
    *,
    checkout_id: uuid.UUID,
    provider_order_id: str,
    provider_payment_id: str,
    signature: str,
    settings: Settings,
) -> VerifyOutcome:
    """Record one client return. Never captures, never creates an order (ADR 0003 D8).

    Runs inside the request's kernel transaction. The order of the four checks is chosen
    so that each one refuses without disclosing what the next would have found:

    1. **Which attempt.** The provider order id is looked up with
       :func:`transaction_kernel.find_attempt_by_provider_order`, which is tenant-scoped,
       so another tenant's order id simply is not there.
    2. **Whose attempt.** :func:`commerce_api.deps.assert_owner` on the attempt's
       checkout. A callback naming somebody else's order is a 404, never a 403: a 403
       would confirm the order id is real, which turns this endpoint into an oracle for
       harvesting live Razorpay order ids.
    3. **Whose signature.** ``HMAC(order_id|payment_id)`` against the API key secret, in
       constant time. A failure is a 401 and nothing is written -- not an audit row, not
       an inbox row, nothing an unauthenticated caller can cause to accumulate.
    4. **What the kernel says.** :func:`transaction_kernel.record_browser_callback`
       stores the payment id if none is held, accepts a repeat of the same one, and
       refuses a different one. It audits every call, accepted or refused, because a
       refused callback is evidence too.

    A reconciliation is enqueued only when this call is the one that recorded the payment
    id. A repeat has already had its reconciliation enqueued in the transaction that
    recorded it, and a refusal recorded nothing to reconcile; enqueuing on either would
    let a caller holding one valid signature grow the outbox without bound.
    """
    ctx.require("payment.verify")

    attempt = tk.find_attempt_by_provider_order(
        session, tenant_id=ctx.tenant_id, provider_order_id=provider_order_id
    )
    if attempt is None or attempt.checkout_id != checkout_id:
        raise ProblemError(
            404,
            "Payment attempt not found",
            "No payment attempt for this session carries that Razorpay order id.",
            razorpay_order_id=provider_order_id,
        )

    # Ownership before signature: the session is the authority on whose money this is,
    # and a signature says nothing about that.
    assert_owner(session, ctx, attempt.checkout_id)

    razorpay = settings.razorpay()
    if not verify_payment_signature(
        order_id=provider_order_id,
        payment_id=provider_payment_id,
        signature=signature,
        secret=razorpay.key_secret,
    ):
        raise ProblemError(
            401,
            "Signature verification failed",
            "razorpay_signature does not verify against this order and payment.",
            razorpay_order_id=provider_order_id,
        )

    verdict = tk.record_browser_callback(
        session,
        tenant_id=ctx.tenant_id,
        payment_attempt_id=attempt.attempt_id,
        provider_order_id=provider_order_id,
        provider_payment_id=provider_payment_id,
        correlation_id=ctx.correlation_id,
        principal_id=ctx.principal.principal_id,
    )

    enqueued = False
    if verdict.accepted and verdict.code is RecoveryCode.OK:
        enqueue_command(
            session,
            ReconcilePaymentCommand(
                tenant_id=str(ctx.tenant_id),
                payment_attempt_id=str(attempt.attempt_id),
                reason="browser_callback",
                attempt_number=1,
                correlation_id=str(ctx.correlation_id),
            ),
            idempotency_key=None,
        )
        enqueued = True

    return VerifyOutcome(
        accepted=verdict.accepted,
        attempt_id=attempt.attempt_id,
        state=verdict.state,
        code=verdict.code,
        reason=verdict.reason,
        enqueued_reconciliation=enqueued,
    )


def order_row_for_attempt(
    session: Session, *, tenant_id: uuid.UUID, attempt_id: uuid.UUID
) -> uuid.UUID | None:
    """The confirmed order for an attempt, or ``None``. Used to prove D8 in tests and to
    fill ``CheckoutOut.order_id`` on the read path."""
    row = session.execute(_ORDER_EVIDENCE, {"t": tenant_id, "a": attempt_id}).one_or_none()
    return None if row is None else uuid.UUID(str(row.id))
