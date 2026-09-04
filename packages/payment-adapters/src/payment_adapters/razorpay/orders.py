"""Razorpay Orders: building the create request, and reading the answer honestly.

Specification 11.1 and 10.6.

Two things happen in this module and they are kept apart on purpose.

:func:`build_create_order_request` is pure. Given a config and an order it returns the
exact bytes and headers that will go on the wire, and it never touches a transport. That
is what lets a test assert the payload byte-for-byte, and what lets a reviewer read the
provider contract without reading an HTTP client.

:func:`create_order` sends that request and classifies what comes back. Every branch it
takes answers one question -- *does a provider order now exist?* -- and the answer is
"unknown" unless the provider said something that rules it out.

The receipt
-----------
``receipt`` is not decoration. Specification 10.6 makes it the lookup key used to find an
order after a create call loses its response, which is the only thing standing between a
timeout and two provider orders for one checkout version. Razorpay caps it at 40
characters.

This module **refuses** an over-long receipt rather than truncating it. Truncation is the
tempting fix and it is the dangerous one: two distinct receipts that share their first 40
characters become the same string, the lookup returns the wrong order, and the platform
concludes that a payment for checkout A settles checkout B. A refusal is a bug report at
build time; a truncation is a silent collision at recovery time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import Money, canonicalize
from transaction_kernel.recovery import RecoveryCode
from transaction_kernel.states import PaymentState

from .config import RazorpayConfig
from .errors import RequestConstructionError
from .transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportError,
    classify_failure,
    parse_json_body,
)

__all__ = [
    "MAX_NOTES_PAIRS",
    "MAX_NOTE_VALUE_LENGTH",
    "MAX_RECEIPT_LENGTH",
    "CreateOrderResult",
    "build_create_order_request",
    "build_order_lookup_request",
    "create_order",
    "find_order_by_receipt",
]

#: Razorpay's documented cap on ``receipt``. The ``payment_attempts.receipt`` column is
#: ``String(40)`` for the same reason, so a value this module accepts always stores.
MAX_RECEIPT_LENGTH: Final[int] = 40

#: Razorpay's documented caps on ``notes``. Enforced here rather than in the platform
#: core, per specification 12.5: provider constraints are adapter-enforced.
MAX_NOTES_PAIRS: Final[int] = 15
MAX_NOTE_VALUE_LENGTH: Final[int] = 512
MAX_NOTE_KEY_LENGTH: Final[int] = 256

#: Smallest amount Razorpay accepts, in minor units, for currencies we have confirmed.
#: A currency absent from this table is simply not checked here -- an unverified minimum
#: would reject legitimate orders, and the provider's own 400 is classified safely as
#: ``PAYMENT_FAILED`` anyway. Only INR is confirmed for the reference flow.
_MINIMUM_AMOUNT_MINOR: Final[Mapping[str, int]] = {"INR": 100}

_JSON_HEADERS: Final[Mapping[str, str]] = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _validate_receipt(receipt: str) -> None:
    """Enforce the provider's receipt contract. Refuses; never repairs."""
    if not receipt:
        raise RequestConstructionError(
            "receipt must not be empty: it is the lookup key that finds an existing "
            "order after a lost create response (specification 10.6)"
        )
    if len(receipt) > MAX_RECEIPT_LENGTH:
        raise RequestConstructionError(
            f"receipt is {len(receipt)} characters; Razorpay accepts at most "
            f"{MAX_RECEIPT_LENGTH}. It is not truncated here: two receipts sharing a "
            "prefix would collide and the recovery lookup would return the wrong order"
        )
    if receipt.strip() != receipt:
        # A trailing space survives our comparison but may not survive the provider's,
        # which would break lookup-by-receipt in exactly the situation it exists for.
        raise RequestConstructionError("receipt must not have leading or trailing whitespace")


def _validate_amount(amount: Money) -> None:
    if amount.minor <= 0:
        raise RequestConstructionError(
            f"order amount must be positive, got {amount}; a zero or negative order is "
            "not a payment and the payment_attempts.amount_positive constraint refuses it"
        )
    minimum = _MINIMUM_AMOUNT_MINOR.get(amount.currency)
    if minimum is not None and amount.minor < minimum:
        raise RequestConstructionError(
            f"Razorpay rejects amounts below {minimum} minor units in {amount.currency}; "
            f"got {amount.minor}"
        )


def _validate_notes(notes: Mapping[str, str]) -> None:
    if len(notes) > MAX_NOTES_PAIRS:
        raise RequestConstructionError(
            f"notes carries {len(notes)} pairs; Razorpay accepts at most {MAX_NOTES_PAIRS}"
        )
    for key, value in notes.items():
        if not isinstance(key, str) or not isinstance(value, str):
            # A non-string value would be coerced by some clients and rejected by others.
            # Refusing keeps the payload identical wherever it is built.
            raise RequestConstructionError(
                f"notes must map str to str; {key!r} ({type(key).__name__}) maps to "
                f"{value!r} ({type(value).__name__})"
            )
        if len(key) > MAX_NOTE_KEY_LENGTH or len(value) > MAX_NOTE_VALUE_LENGTH:
            raise RequestConstructionError(f"notes entry {key!r} exceeds the provider length cap")


def build_create_order_request(
    config: RazorpayConfig,
    *,
    amount: Money,
    receipt: str,
    notes: Mapping[str, str] | None = None,
) -> HttpRequest:
    """Build the exact ``POST /v1/orders`` request. Pure: no I/O, no clock, no randomness.

    The body carries exactly four keys -- ``amount``, ``currency``, ``receipt``, and
    ``notes`` when non-empty -- serialized with :func:`commerce_domain.canonicalize`.
    Canonical serialization gives byte-stable output across processes, and its
    integer-only number profile means a float amount that somehow escaped :class:`Money`
    raises at build time instead of being rounded into a charge.

    ``notes`` is omitted when empty rather than sent as ``{}``, so the request contains
    nothing the platform did not decide to send.

    Guarantees the request is one the provider can accept: positive amount above the
    known per-currency minimum, a receipt within 40 characters, and notes within the
    documented caps.

    Refuses -- ``RequestConstructionError`` -- an empty or over-long receipt, a
    non-positive amount, and oversized or non-string notes. It never truncates or coerces
    a value to make it fit.

    No idempotency header is sent. Razorpay does not offer one on order creation; the
    stable ``receipt`` is the deduplication mechanism (specification 11.1), and the
    single-non-terminal-attempt index in ``payment_attempts`` is what actually enforces
    one order per checkout version.
    """
    _validate_receipt(receipt)
    _validate_amount(amount)
    notes = dict(notes or {})
    _validate_notes(notes)

    payload: dict[str, Any] = {
        "amount": amount.minor,
        "currency": amount.currency,
        "receipt": receipt,
    }
    if notes:
        payload["notes"] = notes

    return HttpRequest(
        method="POST",
        url=config.orders_url(),
        headers=dict(_JSON_HEADERS),
        body=canonicalize(payload),
        auth=(config.key_id, config.key_secret),
    )


def build_order_lookup_request(config: RazorpayConfig, *, receipt: str) -> HttpRequest:
    """Build the ``GET /v1/orders?receipt=...`` recovery lookup.

    Specification 10.6 requires this call after a create-order times out, **before** any
    second create. It is the whole reason the receipt is stable and unique per tenant.
    """
    _validate_receipt(receipt)
    return HttpRequest(
        method="GET",
        url=config.order_lookup_url(receipt),
        headers={"Accept": "application/json"},
        auth=(config.key_id, config.key_secret),
    )


@dataclass(frozen=True, slots=True)
class CreateOrderResult:
    """What the platform now knows about the provider order.

    ``code`` says what the caller may do. ``payment_state`` says what the attempt row
    should hold, and is always a state legally reachable from ``CREATED``.
    """

    code: RecoveryCode
    payment_state: PaymentState
    order_id: str | None
    http_status: int | None
    provider_error_code: str | None = None

    @property
    def order_exists(self) -> bool:
        """True only when the provider confirmed an order identifier."""
        return self.order_id is not None

    @property
    def must_reconcile(self) -> bool:
        """True when a second create is forbidden until a receipt lookup has run.

        Specification 10.7: never turn an unknown outcome into a retry.
        """
        return self.code is RecoveryCode.PAYMENT_UNKNOWN


def _unknown(status: int | None, error_code: str | None = None) -> CreateOrderResult:
    return CreateOrderResult(
        code=RecoveryCode.PAYMENT_UNKNOWN,
        payment_state=PaymentState.UNKNOWN,
        order_id=None,
        http_status=status,
        provider_error_code=error_code,
    )


def _provider_error_code(body: dict[str, Any] | None) -> str | None:
    """Extract Razorpay's ``error.code`` for logs, defensively.

    The error envelope is nested and any level of it may be missing or the wrong type in
    a degraded response; a ``TypeError`` here must not become the outcome of a payment.
    """
    if not body:
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _verify_echo(body: dict[str, Any], *, amount: Money, receipt: str) -> str | None:
    """Return a reason the provider's order does not match what we asked for, or ``None``.

    Razorpay echoes ``amount``, ``currency`` and ``receipt``. Checking them is not
    paranoia about the provider; it is how a receipt collision, a mis-built payload or a
    response routed from another request is caught *before* the buyer is sent to a
    checkout that charges a different number than the one they approved.
    """
    if body.get("amount") != amount.minor:
        return f"amount {body.get('amount')!r} does not match requested {amount.minor}"
    if body.get("currency") != amount.currency:
        return f"currency {body.get('currency')!r} does not match requested {amount.currency}"
    if body.get("receipt") != receipt:
        return f"receipt {body.get('receipt')!r} does not match requested {receipt!r}"
    return None


def create_order(
    transport: HttpTransport,
    config: RazorpayConfig,
    *,
    amount: Money,
    receipt: str,
    notes: Mapping[str, str] | None = None,
) -> CreateOrderResult:
    """Create one Razorpay order and classify the outcome. Sends exactly one request.

    Guarantees:

    * a result of ``OK`` means the provider returned an order identifier **and** echoed
      back the same amount, currency and receipt that were sent;
    * a transport timeout or connection failure yields ``PAYMENT_UNKNOWN`` and
      ``PaymentState.UNKNOWN``, never ``PAYMENT_FAILED``. The order may exist and must be
      found with :func:`find_order_by_receipt` before any second create;
    * a 2xx whose body cannot be read, or which omits ``id``, is likewise unknown;
    * a mismatch between what was requested and what was echoed yields
      ``HUMAN_REVIEW_REQUIRED`` and never advances the attempt, because continuing would
      send the buyer to a payment surface for an amount they did not approve;
    * ``payment_state`` is always a legal successor of ``CREATED``.

    Refuses to retry internally. One call, one request; a retry is a new admission and a
    new single-use Execution Grant.
    """
    request = build_create_order_request(config, amount=amount, receipt=receipt, notes=notes)

    try:
        response: HttpResponse = transport.send(request)
    except TransportError:
        # No response at all. The provider may have written the order before the
        # connection died, so this is the canonical unknown outcome.
        return _unknown(None)

    body = parse_json_body(response)

    if not response.is_success:
        code = classify_failure(response.status)
        error_code = _provider_error_code(body)
        if code is RecoveryCode.PAYMENT_UNKNOWN:
            return _unknown(response.status, error_code)
        # A refused request created nothing, so the attempt may legally fail. The code
        # still distinguishes an operator fault from a payment fault.
        return CreateOrderResult(
            code=code,
            payment_state=PaymentState.FAILED,
            order_id=None,
            http_status=response.status,
            provider_error_code=error_code,
        )

    if body is None:
        return _unknown(response.status)

    order_id = body.get("id")
    if not isinstance(order_id, str) or not order_id:
        # A success status with no identifier: the order probably exists and we cannot
        # name it. Reconciliation by receipt is the only honest next step.
        return _unknown(response.status)

    mismatch = _verify_echo(body, amount=amount, receipt=receipt)
    if mismatch is not None:
        return CreateOrderResult(
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
            payment_state=PaymentState.UNKNOWN,
            order_id=order_id,
            http_status=response.status,
            provider_error_code=f"echo_mismatch: {mismatch}",
        )

    return CreateOrderResult(
        code=RecoveryCode.OK,
        payment_state=PaymentState.SUBMITTED,
        order_id=order_id,
        http_status=response.status,
    )


def find_order_by_receipt(
    transport: HttpTransport,
    config: RazorpayConfig,
    *,
    receipt: str,
    amount: Money,
) -> CreateOrderResult:
    """Recover from an unknown create-order outcome by looking the order up.

    Specification 10.6: this runs *before* any second create. Sends exactly one request.

    Guarantees:

    * exactly one matching order yields ``OK`` and its identifier, but only after the
      same echo check :func:`create_order` applies -- a lookup that returns an order for
      a different amount is a collision, not a recovery;
    * **no** matching order yields ``PAYMENT_FAILED``: the provider has affirmatively
      told us the receipt is unused, which is the verified absence that permits a fresh
      attempt under a new grant;
    * more than one matching order yields ``HUMAN_REVIEW_REQUIRED``. Receipts are unique
      per tenant by database constraint, so duplicates mean the invariant is already
      broken and no automated choice between them is safe;
    * any failure to obtain a clean answer stays ``PAYMENT_UNKNOWN``.
    """
    request = build_order_lookup_request(config, receipt=receipt)

    try:
        response = transport.send(request)
    except TransportError:
        return _unknown(None)

    if not response.is_success:
        # A failed lookup tells us nothing about whether the order exists. It is never
        # evidence of absence, so it can never authorize a second create.
        return _unknown(response.status, _provider_error_code(parse_json_body(response)))

    body = parse_json_body(response)
    if body is None:
        return _unknown(response.status)

    items = body.get("items")
    if not isinstance(items, list):
        return _unknown(response.status)

    orders = [item for item in items if isinstance(item, dict) and item.get("receipt") == receipt]

    if not orders:
        return CreateOrderResult(
            code=RecoveryCode.PAYMENT_FAILED,
            payment_state=PaymentState.FAILED,
            order_id=None,
            http_status=response.status,
        )
    if len(orders) > 1:
        return CreateOrderResult(
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
            payment_state=PaymentState.UNKNOWN,
            order_id=None,
            http_status=response.status,
            provider_error_code=f"duplicate_receipt: {len(orders)} orders",
        )

    found = orders[0]
    order_id = found.get("id")
    if not isinstance(order_id, str) or not order_id:
        return _unknown(response.status)

    mismatch = _verify_echo(found, amount=amount, receipt=receipt)
    if mismatch is not None:
        return CreateOrderResult(
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
            payment_state=PaymentState.UNKNOWN,
            order_id=order_id,
            http_status=response.status,
            provider_error_code=f"echo_mismatch: {mismatch}",
        )

    return CreateOrderResult(
        code=RecoveryCode.OK,
        payment_state=PaymentState.SUBMITTED,
        order_id=order_id,
        http_status=response.status,
    )
