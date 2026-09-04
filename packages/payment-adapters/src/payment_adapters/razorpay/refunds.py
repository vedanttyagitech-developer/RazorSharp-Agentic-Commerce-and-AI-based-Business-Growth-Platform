"""Refunds: full, partial, repeatable, and impossible to issue twice by accident.

Specification 10.6, 10.9 and 11.4.

Two failure states, and they are not interchangeable
----------------------------------------------------
This is the distinction the whole module is built around:

===================  =============================================  =========================
State                What it means                                  What may happen next
===================  =============================================  =========================
``REFUND_UNKNOWN``   The provider's answer was lost. A refund may    **Reconcile only.** Fetch
                     already exist.                                 the payment's refunds by
                                                                    authoritative identifier;
                                                                    only verified absence
                                                                    permits a new attempt.
``REFUND_FAILED``    The provider confirmed the refund failed. No    Retry under a **new**
                     refund exists.                                 Execution Grant, where
                                                                    merchant policy allows.
===================  =============================================  =========================

Conflating them is how a buyer gets refunded twice, so the refusal is structural rather
than advisory: :func:`plan_refund` will not produce an executable plan from
``REFUND_UNKNOWN``, and :func:`execute_refund` refuses to send a plan that was not
allowed. There is no path through this module that retries an unknown refund.

The idempotency key
-------------------
Razorpay accepts ``X-Razorpay-Idempotency-Key`` on refund creation, and the platform
needs a key that is *stable* across transport-level retries of one attempt while being
*distinct* between two genuinely different refunds. Those two requirements pull against
each other in exactly one case: two partial refunds of the same amount against the same
payment -- two ₹100 refunds for two separate damaged items. Keyed on payment and amount
alone they would collide, the provider would return the first refund for the second
request, and the buyer would be short ₹100 while the ledger read as settled.

So the key is derived from payment id, amount, **and a sequence ordinal** taken from the
merchant's own committed refund history for that payment. See
:func:`refund_idempotency_key` for the contract the caller must honour.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from commerce_domain import Money, canonical_hash, canonicalize
from transaction_kernel.recovery import RecoveryCode
from transaction_kernel.states import PaymentState, can_transition

from .config import RazorpayConfig
from .errors import RefundNotPermittedError, RequestConstructionError
from .transport import (
    HttpRequest,
    HttpTransport,
    TransportError,
    classify_failure,
    parse_json_body,
)

__all__ = [
    "IDEMPOTENCY_HEADER",
    "CaptureLedger",
    "RefundDecision",
    "RefundPlan",
    "RefundRefusal",
    "RefundResult",
    "RefundSpeed",
    "build_refund_request",
    "execute_refund",
    "may_retry_after",
    "must_reconcile_before_retry",
    "plan_refund",
    "refund_idempotency_key",
]

IDEMPOTENCY_HEADER: Final[str] = "X-Razorpay-Idempotency-Key"

#: Version tag mixed into the idempotency key. If the derivation ever changes, bumping
#: this makes every new key distinct from every old one, rather than producing a key that
#: collides with a historical refund under a different rule.
_KEY_SCHEME_VERSION: Final[int] = 1


class RefundSpeed(StrEnum):
    """Razorpay's refund speed. ``NORMAL`` settles on the standard cycle."""

    NORMAL = "normal"
    OPTIMUM = "optimum"


class RefundRefusal(StrEnum):
    """Stable reason keys for a refused refund.

    Keys, not sentences, for the same reason ``KernelDecision.explanation`` is: an agent
    renders these into a buyer's language and must not be able to alter what they mean.
    """

    OK = "OK"
    RECONCILE_UNKNOWN_FIRST = "RECONCILE_UNKNOWN_FIRST"
    STATE_FORBIDS_REFUND = "STATE_FORBIDS_REFUND"
    NOTHING_REMAINING = "NOTHING_REMAINING"
    EXCEEDS_REMAINING = "EXCEEDS_REMAINING"
    NON_POSITIVE_AMOUNT = "NON_POSITIVE_AMOUNT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


# ------------------------------------------------------------------------------ ledger


@dataclass(frozen=True, slots=True)
class CaptureLedger:
    """What was captured against one payment and what has already been refunded.

    Both figures come from committed local rows reconciled against the provider, never
    from an in-flight attempt. A refund that is merely ``REFUND_PENDING`` must be counted
    in ``refunded`` by the caller if it wants that money reserved; this module takes the
    numbers it is given and enforces arithmetic on them.
    """

    captured: Money
    refunded: Money

    def __post_init__(self) -> None:
        if self.captured.currency != self.refunded.currency:
            raise RequestConstructionError(
                f"ledger currencies disagree: captured {self.captured.currency}, "
                f"refunded {self.refunded.currency}"
            )
        if self.captured.is_negative or self.refunded.is_negative:
            raise RequestConstructionError("ledger amounts must not be negative")
        if self.refunded > self.captured:
            # Already broken before this refund was considered. Refusing to construct
            # stops the module from computing a "remaining" that would be negative and
            # then reasoning confidently about it.
            raise RequestConstructionError(
                f"refunded {self.refunded} exceeds captured {self.captured}; "
                "the ledger is already inconsistent and needs reconciliation, not a refund"
            )

    @property
    def remaining(self) -> Money:
        """The largest refund this payment can still support."""
        return self.captured - self.refunded


# ------------------------------------------------------------------- idempotency key


def refund_idempotency_key(*, payment_id: str, amount: Money, sequence: int) -> str:
    """Derive the stable idempotency key for one refund attempt.

    Guarantees:

    * **stable** -- the same ``(payment_id, amount, sequence)`` always yields the same
      key, in every process and on every day, so a transport-level retry of the same
      attempt cannot create a second refund;
    * **distinct** -- two refunds of the same amount against the same payment yield
      different keys as long as they carry different ordinals, which is what allows
      repeated partial refunds (specification 11.4) without silent collapse into one.

    The caller's contract for ``sequence``: it is the 1-based ordinal of this refund
    within the payment's own committed refund history, and **a retry of the same logical
    refund must reuse its ordinal**. Deriving it from a count of committed rows satisfies
    both halves; deriving it from an in-memory counter or a timestamp satisfies neither,
    because a process restart would produce a fresh key for a refund that may already
    exist at the provider.

    Refuses a non-positive ordinal, which would collapse two refunds onto one key.
    """
    if sequence < 1:
        raise RequestConstructionError(
            f"refund sequence must be a 1-based ordinal, got {sequence}; a shared ordinal "
            "would give two distinct refunds the same idempotency key"
        )
    if not payment_id:
        raise RequestConstructionError("payment_id is required to derive a refund key")
    digest = canonical_hash(
        {
            "v": _KEY_SCHEME_VERSION,
            "payment_id": payment_id,
            "amount_minor": amount.minor,
            "currency": amount.currency,
            "sequence": sequence,
        }
    )
    return f"rfnd_{digest}"


# -------------------------------------------------------------------------- planning


@dataclass(frozen=True, slots=True)
class RefundPlan:
    """An approved, fully specified refund. Only :func:`plan_refund` produces one."""

    payment_id: str
    #: The exact amount to refund. Always concrete, never ``None``: a "full" refund is
    #: expressed as the remaining amount so that the request says what it means and the
    #: resulting state can be predicted before it is sent.
    amount: Money
    idempotency_key: str
    sequence: int
    is_full_remaining: bool
    resulting_state: PaymentState
    speed: RefundSpeed = RefundSpeed.NORMAL
    notes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefundDecision:
    """Whether this refund may be attempted, and why not when it may not."""

    allowed: bool
    code: RecoveryCode
    explanation: RefundRefusal
    plan: RefundPlan | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.plan is None:
            raise ValueError("an allowed refund decision must carry the plan it approved")
        if not self.allowed and self.plan is not None:
            # A plan attached to a refusal is a plan somebody will eventually execute.
            raise ValueError("a refused refund decision must not carry an executable plan")


def _refuse(code: RecoveryCode, explanation: RefundRefusal) -> RefundDecision:
    return RefundDecision(allowed=False, code=code, explanation=explanation, plan=None)


def must_reconcile_before_retry(state: PaymentState) -> bool:
    """True when the provider's answer is unknown and a retry would risk a double refund.

    Specification 10.6: ``REFUND_UNKNOWN`` and ``UNKNOWN`` are resolved by fetching the
    provider's own records, never by trying again.
    """
    return state in (PaymentState.REFUND_UNKNOWN, PaymentState.UNKNOWN)


def may_retry_after(state: PaymentState) -> bool:
    """True when a failed refund may be retried under a **new** Execution Grant.

    Only ``REFUND_FAILED`` qualifies: the provider confirmed no refund exists. The retry
    is a fresh kernel admission producing a new single-use grant, never a reuse of the
    grant the failed attempt consumed.
    """
    return state is PaymentState.REFUND_FAILED


def plan_refund(
    *,
    payment_id: str,
    current_state: PaymentState,
    ledger: CaptureLedger,
    sequence: int,
    amount: Money | None = None,
    speed: RefundSpeed = RefundSpeed.NORMAL,
    notes: Mapping[str, str] | None = None,
) -> RefundDecision:
    """Decide whether a refund may be attempted, and specify it exactly if so.

    ``amount=None`` requests a full refund, which is resolved to the ledger's remaining
    amount rather than left implicit.

    Guarantees:

    * a refund is **never** planned from ``REFUND_UNKNOWN``. That returns
      ``RECONCILIATION_IN_PROGRESS`` with ``RECONCILE_UNKNOWN_FIRST``, because a refund
      may already exist and issuing a second grant is how a buyer is paid twice
      (specification 10.6);
    * the state must be one from which the payment state machine permits
      ``REFUND_PENDING``. The permitted set is read from
      ``transaction_kernel.states``, not restated here, so the two cannot drift apart;
    * the refunded total can never exceed the captured total, across any number of
      partial refunds (specification 11.4);
    * a zero or negative amount is refused rather than sent as a no-op;
    * ``resulting_state`` is computed before the request is sent, so the caller knows
      whether success means ``REFUNDED`` or ``PARTIALLY_REFUNDED`` without having to
      re-derive it from the provider's answer.

    Returns a refusal rather than raising for every business outcome; the only raises are
    for malformed derivation inputs, which are caller bugs.
    """
    if must_reconcile_before_retry(current_state):
        return _refuse(
            RecoveryCode.RECONCILIATION_IN_PROGRESS,
            RefundRefusal.RECONCILE_UNKNOWN_FIRST,
        )

    # The lifecycle owns which states may open a refund; asking it keeps this adapter
    # from growing a second, divergent copy of that rule.
    if not can_transition(current_state, PaymentState.REFUND_PENDING):
        return _refuse(RecoveryCode.POLICY_EXCEPTION, RefundRefusal.STATE_FORBIDS_REFUND)

    remaining = ledger.remaining
    if remaining.is_zero:
        return _refuse(RecoveryCode.POLICY_EXCEPTION, RefundRefusal.NOTHING_REMAINING)

    requested = remaining if amount is None else amount

    if requested.currency != remaining.currency:
        return _refuse(RecoveryCode.POLICY_EXCEPTION, RefundRefusal.CURRENCY_MISMATCH)
    if requested.minor <= 0:
        return _refuse(RecoveryCode.POLICY_EXCEPTION, RefundRefusal.NON_POSITIVE_AMOUNT)
    if requested > remaining:
        return _refuse(RecoveryCode.POLICY_EXCEPTION, RefundRefusal.EXCEEDS_REMAINING)

    is_full_remaining = requested == remaining
    plan = RefundPlan(
        payment_id=payment_id,
        amount=requested,
        idempotency_key=refund_idempotency_key(
            payment_id=payment_id, amount=requested, sequence=sequence
        ),
        sequence=sequence,
        is_full_remaining=is_full_remaining,
        resulting_state=(
            PaymentState.REFUNDED if is_full_remaining else PaymentState.PARTIALLY_REFUNDED
        ),
        speed=speed,
        notes=dict(notes or {}),
    )
    return RefundDecision(
        allowed=True,
        code=RecoveryCode.REFUND_ALLOWED,
        explanation=RefundRefusal.OK,
        plan=plan,
    )


# --------------------------------------------------------------------------- requests


def build_refund_request(config: RazorpayConfig, plan: RefundPlan) -> HttpRequest:
    """Build ``POST /v1/payments/{id}/refund``. Pure: no I/O.

    The amount is always sent explicitly, even for a full refund. Omitting it would ask
    the provider to refund "whatever is left", which is a different question than the one
    the merchant approved -- and if a refund landed between planning and sending, the two
    answers differ by real money.

    Carries the plan's idempotency key in ``X-Razorpay-Idempotency-Key`` so that a
    transport-level retry of this exact request returns the original refund instead of
    creating a second one.
    """
    payload: dict[str, Any] = {
        "amount": plan.amount.minor,
        "speed": plan.speed.value,
    }
    if plan.notes:
        payload["notes"] = dict(plan.notes)

    return HttpRequest(
        method="POST",
        url=config.refunds_url(plan.payment_id),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            IDEMPOTENCY_HEADER: plan.idempotency_key,
        },
        body=canonicalize(payload),
        auth=(config.key_id, config.key_secret),
    )


# ---------------------------------------------------------------------------- outcome


@dataclass(frozen=True, slots=True)
class RefundResult:
    """What the platform now knows about the refund."""

    code: RecoveryCode
    payment_state: PaymentState
    refund_id: str | None
    http_status: int | None
    provider_status: str | None = None
    provider_error_code: str | None = None

    @property
    def must_reconcile(self) -> bool:
        """True when the outcome is unknown and no retry may be issued.

        The single most important property on this object. ``REFUND_UNKNOWN`` reconciles;
        it never retries.
        """
        return self.payment_state is PaymentState.REFUND_UNKNOWN

    @property
    def may_retry_under_new_grant(self) -> bool:
        """True only for a provider-confirmed failure."""
        return self.payment_state is PaymentState.REFUND_FAILED


#: Razorpay refund entity statuses, and what each means locally. An unrecognised status on
#: an entity that has an id still means the refund *exists*, so it maps to
#: ``REFUND_PENDING`` rather than to anything terminal.
_REFUND_STATUS_STATES: Final[Mapping[str, PaymentState]] = {
    "processed": PaymentState.REFUNDED,
    "pending": PaymentState.REFUND_PENDING,
    "failed": PaymentState.REFUND_FAILED,
}


def _unknown_refund(status: int | None, error_code: str | None = None) -> RefundResult:
    return RefundResult(
        code=RecoveryCode.PAYMENT_UNKNOWN,
        payment_state=PaymentState.REFUND_UNKNOWN,
        refund_id=None,
        http_status=status,
        provider_error_code=error_code,
    )


def _error_code(body: dict[str, Any] | None) -> str | None:
    if not body:
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _verify_refund_echo(body: Mapping[str, Any], plan: RefundPlan) -> str | None:
    """Return a reason the returned refund is not the one that was planned, or ``None``.

    The same check :func:`payment_adapters.razorpay.orders.create_order` performs on an
    order, and it matters more here, because the request carries an idempotency key.

    ``X-Razorpay-Idempotency-Key`` makes the provider return the **original** refund for a
    repeated key instead of creating a second one. That is the behaviour the key exists
    for -- and it is also what happens when a caller supplies an ordinal that a previous,
    *different* refund already used. The provider then answers a Rs395 request with the
    Rs1 refund it made earlier, ``status`` reads ``processed``, and without this check the
    plan's own ``resulting_state`` is applied: ``REFUNDED``, which is terminal. The buyer
    is owed the balance forever and the ledger reads as settled.

    So a refund whose echoed amount or currency is not the one that was planned is not
    treated as this refund's answer at all. ``payment_id`` is checked on the same grounds
    when the provider includes it.
    """
    if body.get("amount") != plan.amount.minor:
        return f"amount {body.get('amount')!r} does not match planned {plan.amount.minor}"
    currency = body.get("currency")
    if isinstance(currency, str) and currency != plan.amount.currency:
        return f"currency {currency!r} does not match planned {plan.amount.currency}"
    echoed_payment = body.get("payment_id")
    if isinstance(echoed_payment, str) and echoed_payment != plan.payment_id:
        return f"payment_id {echoed_payment!r} does not match planned {plan.payment_id!r}"
    return None


def execute_refund(
    transport: HttpTransport,
    config: RazorpayConfig,
    decision: RefundDecision,
) -> RefundResult:
    """Send one approved refund and classify the outcome. Sends exactly one request.

    Guarantees:

    * a refused :class:`RefundDecision` is never sent. Passing one raises
      :class:`RefundNotPermittedError`, so the gate in :func:`plan_refund` cannot be
      stepped around by a caller that forgot to check ``allowed``;
    * a timeout, a connection failure, a 5xx, a 429, an unreadable body or a success
      without a refund identifier all yield ``REFUND_UNKNOWN`` with ``PAYMENT_UNKNOWN`` --
      a code deliberately excluded from ``recovery.RETRYABLE``. The refund may exist and
      must be reconciled against the provider's refund list before any new attempt;
    * only a provider-confirmed refusal yields ``REFUND_FAILED``, which may be retried
      under a new Execution Grant;
    * a successfully processed refund resolves to the ``resulting_state`` the plan
      computed -- ``REFUNDED`` for a full remaining refund, ``PARTIALLY_REFUNDED``
      otherwise -- so a partial refund is never recorded as a complete one;
    * a refund entity echoing an amount, currency or payment other than the one planned
      yields ``HUMAN_REVIEW_REQUIRED`` with ``REFUND_UNKNOWN`` and concludes nothing. The
      idempotency key makes the provider return the *original* refund for a reused key,
      so this is the answer a caller gets when it supplies an ordinal a different refund
      already used -- see :func:`_verify_refund_echo`.
    """
    if not decision.allowed or decision.plan is None:
        raise RefundNotPermittedError(
            f"refund was refused with {decision.explanation.value}; "
            "a refused decision is never executed"
        )
    plan = decision.plan
    request = build_refund_request(config, plan)

    try:
        response = transport.send(request)
    except TransportError:
        return _unknown_refund(None)

    body = parse_json_body(response)

    if not response.is_success:
        code = classify_failure(response.status)
        error_code = _error_code(body)
        if code is RecoveryCode.PAYMENT_UNKNOWN:
            return _unknown_refund(response.status, error_code)
        return RefundResult(
            code=code,
            payment_state=PaymentState.REFUND_FAILED,
            refund_id=None,
            http_status=response.status,
            provider_error_code=error_code,
        )

    if body is None:
        return _unknown_refund(response.status)

    refund_id = body.get("id")
    if not isinstance(refund_id, str) or not refund_id:
        # A 2xx with no identifier: the refund very likely exists and we cannot name it.
        return _unknown_refund(response.status)

    mismatch = _verify_refund_echo(body, plan)
    if mismatch is not None:
        # Not the refund we planned. The identifier is kept so reconciliation has an
        # authoritative handle, but no state is concluded from it: REFUND_UNKNOWN
        # reconciles and never retries, so this can neither strand the balance in a
        # terminal REFUNDED nor authorize a second refund.
        return RefundResult(
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
            payment_state=PaymentState.REFUND_UNKNOWN,
            refund_id=refund_id,
            http_status=response.status,
            provider_error_code=f"echo_mismatch: {mismatch}",
        )

    provider_status = body.get("status")
    provider_status = provider_status if isinstance(provider_status, str) else None
    mapped = _REFUND_STATUS_STATES.get(provider_status or "", PaymentState.REFUND_PENDING)

    if mapped is PaymentState.REFUNDED:
        # The provider says this refund settled. Whether the *payment* is now fully
        # refunded is a fact about the ledger, not about this one refund, and the plan
        # already worked it out against the remaining balance.
        state = plan.resulting_state
        code = RecoveryCode.OK
    elif mapped is PaymentState.REFUND_FAILED:
        state = PaymentState.REFUND_FAILED
        code = RecoveryCode.PAYMENT_FAILED
    else:
        state = PaymentState.REFUND_PENDING
        code = RecoveryCode.PAYMENT_PENDING

    return RefundResult(
        code=code,
        payment_state=state,
        refund_id=refund_id,
        http_status=response.status,
        provider_status=provider_status,
    )
