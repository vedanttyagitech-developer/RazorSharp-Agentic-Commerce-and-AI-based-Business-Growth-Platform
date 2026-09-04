"""Reading provider payment state by authoritative identifier.

Specification 10.7 and 11.2. This is the server-side half of "never trust the browser":
after a create-order response is lost, a client return arrives, or a webhook is missing,
the platform asks Razorpay what actually happened to the money and moves the attempt only
from what comes back. The evidence produced here carries ``source = "PROVIDER_FETCH"``,
one of the two channels (with a verified webhook) that
``transaction_kernel.payments.apply_provider_evidence`` accepts for capture (ADR 0003 D8).

Two fetches, one discipline
---------------------------
:func:`fetch_payment` reads one payment by its identifier -- the path after a signed
browser callback names the payment. :func:`fetch_order_payments` lists every payment on
an order -- the path when only the order is known, which is every reconciliation that
starts from a lost create-order response. Both send exactly one request, both classify the
answer through the same allowlist, and both refuse to interpret an entity that does not
echo the amount, currency and order the platform recorded.

Why a mismatch raises instead of returning a state
--------------------------------------------------
The create-order path (:mod:`.orders`) reports an echo mismatch as
``HUMAN_REVIEW_REQUIRED`` because no money has moved yet and the attempt can simply not
proceed. Here money may already have moved, and the fetched entity is about to be recorded
as evidence. A payment for a different amount or a different order is not evidence about
*this* attempt at all; folding it into any state would let the kernel mark one checkout
paid with another checkout's capture. So it is raised as :class:`EvidenceMismatchError`,
which the worker must escalate, and never becomes a value the kernel could apply.

Why a refused fetch is not a failed payment
-------------------------------------------
:func:`transport.classify_failure` maps 400/404/422 to ``PAYMENT_FAILED`` because for a
*mutation* those statuses prove nothing was created. For a *read* they prove nothing about
the money: a 404 on ``GET /v1/payments/{id}`` says the identifier the platform recorded is
unknown to the provider, which is an inconsistency an operator must look at, not a
confirmation that the buyer's card was declined. So those statuses become
``HUMAN_REVIEW_REQUIRED`` here. Timeouts, 5xx and unreadable bodies stay
``PAYMENT_UNKNOWN`` exactly as the shared classifier says, and the caller's bounded
reconciliation (ADR 0003 D13) decides when to stop asking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from urllib.parse import quote

from commerce_domain import Money, sha256_hex
from transaction_kernel.recovery import RecoveryCode
from transaction_kernel.states import PaymentState

from .config import RazorpayConfig
from .errors import EvidenceMismatchError, RequestConstructionError
from .transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportError,
    classify_failure,
    parse_json_body,
)

__all__ = [
    "EVIDENCE_SOURCE",
    "FetchClassification",
    "OrderPaymentsResult",
    "PaymentFetchResult",
    "ProviderEvidence",
    "build_fetch_payment_request",
    "build_order_payments_request",
    "fetch_order_payments",
    "fetch_payment",
]

#: The evidence channel every result of this module carries. Mirrors
#: ``CaptureEvidence.PROVIDER_FETCH`` by value so the kernel can compare against the
#: string without importing this package (ADR 0003 D2).
EVIDENCE_SOURCE: Final[str] = "PROVIDER_FETCH"


class FetchClassification(StrEnum):
    """What a fetch established about the payment. Values are the evidence ``status``.

    ``UNKNOWN`` is the only member that never appears on a :class:`ProviderEvidence`: it
    means no evidence was obtained, and the ``RecoveryCode`` on the result says why.
    """

    CAPTURED = "captured"
    AUTHORIZED = "authorized"
    FAILED = "failed"
    PENDING = "pending"
    UNKNOWN = "unknown"


#: Razorpay payment entity statuses and their local classification. An allowlist: a status
#: absent from here is *not* guessed, because a new provider status could mean anything
#: from "on hold" to "disputed" and the adapter has no business inventing a meaning for it.
#:
#: ``refunded`` classifies as captured on purpose. Money was captured and then returned;
#: the capture is a fact this attempt must record, and ``amount_refunded_minor`` on the
#: evidence carries the refund for the kernel's refund reconciliation to act on.
_PROVIDER_STATUS_CLASSIFICATION: Final[Mapping[str, FetchClassification]] = {
    "captured": FetchClassification.CAPTURED,
    "authorized": FetchClassification.AUTHORIZED,
    "failed": FetchClassification.FAILED,
    "created": FetchClassification.PENDING,
    "pending": FetchClassification.PENDING,
    "refunded": FetchClassification.CAPTURED,
}

#: The code a verified classification reports. ``PAYMENT_FAILED`` is retryable because a
#: provider-confirmed failed payment is exactly the "verified absence" that permits a new
#: admission; ``PAYMENT_PENDING`` is not, because the buyer may still be on the surface.
_CLASSIFICATION_CODE: Final[Mapping[FetchClassification, RecoveryCode]] = {
    FetchClassification.CAPTURED: RecoveryCode.OK,
    FetchClassification.AUTHORIZED: RecoveryCode.OK,
    FetchClassification.FAILED: RecoveryCode.PAYMENT_FAILED,
    FetchClassification.PENDING: RecoveryCode.PAYMENT_PENDING,
}

#: The attempt state a classification supports. ``PENDING`` maps to nothing: a payment the
#: buyer has not finished is not a transition, and the attempt stays where it is.
_CLASSIFICATION_STATE: Final[Mapping[FetchClassification, PaymentState]] = {
    FetchClassification.CAPTURED: PaymentState.CAPTURED,
    FetchClassification.AUTHORIZED: PaymentState.AUTHORIZED,
    FetchClassification.FAILED: PaymentState.FAILED,
}

#: Which payment decides an order's classification when it carries several. Money that
#: moved outranks money that did not, and a live attempt outranks a dead one, so a failed
#: first try followed by a captured second try reads as captured.
_DECISIVE_RANK: Final[Mapping[FetchClassification, int]] = {
    FetchClassification.FAILED: 0,
    FetchClassification.PENDING: 1,
    FetchClassification.AUTHORIZED: 2,
    FetchClassification.CAPTURED: 3,
}

#: Classifications in which the provider is holding or has taken the buyer's money. Two
#: of these on one order is a double charge, not a choice to make automatically.
_MONEY_HELD: Final[frozenset[FetchClassification]] = frozenset(
    {FetchClassification.AUTHORIZED, FetchClassification.CAPTURED}
)

_ACCEPT_JSON: Final[Mapping[str, str]] = {"Accept": "application/json"}


# ----------------------------------------------------------------------------- evidence


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    """One payment as the provider reported it, in provider-neutral primitives.

    This is the shape ``transaction_kernel.payments.apply_provider_evidence`` accepts. It
    is deliberately a dataclass of ``str``, ``int`` and ``None`` with no enum and no
    ``Money`` so the kernel can mirror it field for field without importing this package
    (ADR 0003 D2). Every field is read from the fetched entity; nothing is inferred.

    Fields
    ------
    ``source``
        Always ``"PROVIDER_FETCH"`` (:data:`EVIDENCE_SOURCE`). A webhook produces the same
        shape with ``"WEBHOOK"``; the kernel gates capture on the source, never on trust in
        the caller.
    ``provider_payment_id`` / ``provider_order_id``
        Razorpay's ``pay_…`` and ``order_…`` identifiers, exactly as echoed.
    ``amount_minor`` / ``currency``
        Integer minor units and ISO code, already verified equal to what the attempt
        recorded -- a caller never needs to re-check them.
    ``status``
        One of ``"authorized"``, ``"captured"``, ``"failed"``, ``"pending"``. Never
        ``"unknown"``: an unknown fetch produces no evidence at all.
    ``provider_status``
        The raw Razorpay status string (``created``, ``authorized``, ``captured``,
        ``refunded``, ``failed``), kept so a reviewer can see why ``status`` says what it
        says -- in particular that a ``refunded`` payment is recorded as a capture.
    ``captured_at`` / ``authorized_at`` / ``created_at``
        Unix epoch seconds as Razorpay emits them, or ``None`` when the entity did not
        carry the field. Razorpay's payment entity documents ``created_at`` only; the
        other two are read when present and never synthesised from a local clock.
    ``amount_refunded_minor``
        Razorpay's ``amount_refunded`` when present. Non-zero on a captured payment means
        the refund ledger must be reconciled too.
    ``method``, ``error_code``, ``error_reason``
        Descriptive fields for the inspector document and the audit row; ``error_*`` are
        populated on failed payments.
    ``raw_digest``
        Lowercase hex SHA-256 of the raw response body bytes, so the audit row can prove
        which bytes this evidence was read from without storing them twice.
    ``http_status``
        The status the provider answered with; always 2xx on an evidence object.
    """

    source: str
    provider_payment_id: str
    provider_order_id: str
    amount_minor: int
    currency: str
    status: str
    provider_status: str
    captured_at: int | None
    authorized_at: int | None
    created_at: int | None
    amount_refunded_minor: int | None
    method: str | None
    error_code: str | None
    error_reason: str | None
    raw_digest: str
    http_status: int

    @property
    def classification(self) -> FetchClassification:
        return FetchClassification(self.status)

    @property
    def payment_state(self) -> PaymentState | None:
        """The attempt state this evidence supports, or ``None`` for a pending payment."""
        return _CLASSIFICATION_STATE.get(self.classification)


# ------------------------------------------------------------------------------ results


@dataclass(frozen=True, slots=True)
class PaymentFetchResult:
    """What one ``GET /v1/payments/{id}`` established.

    ``classification`` is ``UNKNOWN`` exactly when ``evidence`` is ``None``; ``code`` then
    says whether to keep reconciling (``PAYMENT_UNKNOWN``) or to stop and escalate
    (``HUMAN_REVIEW_REQUIRED``).
    """

    code: RecoveryCode
    classification: FetchClassification
    evidence: ProviderEvidence | None
    http_status: int | None
    provider_error_code: str | None = None

    @property
    def is_verified(self) -> bool:
        """True when the provider answered with a readable, echo-checked payment."""
        return self.evidence is not None

    @property
    def must_reconcile(self) -> bool:
        """True when nothing was learned and the fetch should be repeated, bounded."""
        return self.code is RecoveryCode.PAYMENT_UNKNOWN

    @property
    def payment_state(self) -> PaymentState | None:
        """The attempt state this result supports, or ``None`` to leave the attempt as is.

        Every non-``None`` value is a legal successor of ``RECONCILING``, which is where an
        attempt necessarily sits while a fetch runs.
        """
        return _CLASSIFICATION_STATE.get(self.classification)


@dataclass(frozen=True, slots=True)
class OrderPaymentsResult:
    """What one ``GET /v1/orders/{id}/payments`` established.

    ``payments`` holds every payment on the order in provider order, each already
    echo-checked. ``evidence`` is the decisive one -- the payment that has progressed
    furthest -- and ``classification`` is its status, or ``PENDING`` with no evidence
    when the order carries no payment yet.
    """

    code: RecoveryCode
    classification: FetchClassification
    evidence: ProviderEvidence | None
    payments: tuple[ProviderEvidence, ...]
    http_status: int | None
    provider_error_code: str | None = None

    @property
    def is_verified(self) -> bool:
        """True when the provider answered with a readable, echo-checked list.

        An empty list is verified: the provider affirmatively said no payment exists yet.
        """
        return self.classification is not FetchClassification.UNKNOWN

    @property
    def must_reconcile(self) -> bool:
        return self.code is RecoveryCode.PAYMENT_UNKNOWN

    @property
    def payment_state(self) -> PaymentState | None:
        return _CLASSIFICATION_STATE.get(self.classification)


# ----------------------------------------------------------------------------- requests


def _validate_identifier(value: str, *, name: str) -> None:
    """Refuse an identifier that would change the route rather than address a resource.

    The identifier is percent-encoded when the URL is built, so a ``/`` could not escape
    the path segment anyway; it is refused rather than encoded because no genuine Razorpay
    identifier contains one, and encoding it would send a request for a resource that
    cannot exist instead of reporting the bug at the call site.
    """
    if not value:
        raise RequestConstructionError(f"{name} must not be empty; a fetch needs an identifier")
    if any(ch.isspace() for ch in value) or "/" in value:
        raise RequestConstructionError(
            f"{name} {value!r} contains whitespace or '/'; no Razorpay identifier does"
        )


def build_fetch_payment_request(config: RazorpayConfig, payment_id: str) -> HttpRequest:
    """Build the exact ``GET /v1/payments/{id}`` request. Pure: no I/O, no clock.

    Refuses -- ``RequestConstructionError`` -- an empty identifier or one containing
    whitespace or ``/``. The identifier is percent-encoded into the path so it can never
    be read as a query string or a second segment.
    """
    _validate_identifier(payment_id, name="payment_id")
    return HttpRequest(
        method="GET",
        url=f"{config.base_url}/payments/{quote(payment_id, safe='')}",
        headers=dict(_ACCEPT_JSON),
        auth=(config.key_id, config.key_secret),
    )


def build_order_payments_request(config: RazorpayConfig, order_id: str) -> HttpRequest:
    """Build the exact ``GET /v1/orders/{id}/payments`` request. Pure.

    This is the reconciliation read specification 10.7 step 4 names when only the order
    is known: every payment the buyer attempted against the order, whatever state each
    reached, so that a failed first try cannot hide a captured second one.
    """
    _validate_identifier(order_id, name="order_id")
    return HttpRequest(
        method="GET",
        url=f"{config.base_url}/orders/{quote(order_id, safe='')}/payments",
        headers=dict(_ACCEPT_JSON),
        auth=(config.key_id, config.key_secret),
    )


# --------------------------------------------------------------------------- reading


@dataclass(frozen=True, slots=True)
class _Identity:
    """The fields a payment entity must carry before anything else about it is read."""

    payment_id: str
    order_id: str
    amount: int
    currency: str
    provider_status: str


def _optional_str(entity: Mapping[str, Any], key: str) -> str | None:
    value = entity.get(key)
    return value if isinstance(value, str) and value else None


def _optional_int(entity: Mapping[str, Any], key: str) -> int | None:
    """Read an integer field, refusing ``bool`` -- which is an ``int`` to ``isinstance``."""
    value = entity.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _read_identity(entity: Mapping[str, Any]) -> _Identity | None:
    """Return the entity's identifying facts, or ``None`` if any is missing or mistyped.

    ``None`` means "unreadable" and is classified as unknown: an entity without an amount
    or a status is not one the adapter can vouch for, and guessing a default for either
    would be inventing evidence.
    """
    payment_id = _optional_str(entity, "id")
    order_id = _optional_str(entity, "order_id")
    amount = _optional_int(entity, "amount")
    currency = _optional_str(entity, "currency")
    provider_status = _optional_str(entity, "status")
    if (
        payment_id is None
        or order_id is None
        or amount is None
        or currency is None
        or provider_status is None
    ):
        return None
    return _Identity(payment_id, order_id, amount, currency, provider_status)


def _check_echo(
    identity: _Identity,
    *,
    expected_payment_id: str | None,
    expected_order_id: str,
    expected_amount: Money,
) -> None:
    """Refuse an entity that is not about the attempt it was fetched for.

    Raises :class:`EvidenceMismatchError`. See the module docstring for why this is a
    raise and not a result: the entity may be perfectly valid evidence about some other
    checkout, and the one thing the platform must not do is record it against this one.
    """
    if expected_payment_id is not None and identity.payment_id != expected_payment_id:
        raise EvidenceMismatchError(
            f"provider returned payment {identity.payment_id!r} for a fetch of "
            f"{expected_payment_id!r}"
        )
    if identity.order_id != expected_order_id:
        raise EvidenceMismatchError(
            f"payment {identity.payment_id!r} belongs to order {identity.order_id!r}, "
            f"not to the attempt's order {expected_order_id!r}"
        )
    if identity.amount != expected_amount.minor:
        raise EvidenceMismatchError(
            f"payment {identity.payment_id!r} is for amount {identity.amount}, not the "
            f"attempt's {expected_amount.minor}"
        )
    if identity.currency != expected_amount.currency:
        raise EvidenceMismatchError(
            f"payment {identity.payment_id!r} is in {identity.currency!r}, not the "
            f"attempt's {expected_amount.currency!r}"
        )


def _evidence(
    entity: Mapping[str, Any],
    identity: _Identity,
    classification: FetchClassification,
    *,
    raw_digest: str,
    http_status: int,
) -> ProviderEvidence:
    return ProviderEvidence(
        source=EVIDENCE_SOURCE,
        provider_payment_id=identity.payment_id,
        provider_order_id=identity.order_id,
        amount_minor=identity.amount,
        currency=identity.currency,
        status=classification.value,
        provider_status=identity.provider_status,
        captured_at=_optional_int(entity, "captured_at"),
        authorized_at=_optional_int(entity, "authorized_at"),
        created_at=_optional_int(entity, "created_at"),
        amount_refunded_minor=_optional_int(entity, "amount_refunded"),
        method=_optional_str(entity, "method"),
        error_code=_optional_str(entity, "error_code"),
        error_reason=_optional_str(entity, "error_reason"),
        raw_digest=raw_digest,
        http_status=http_status,
    )


def _provider_error_code(body: Mapping[str, Any] | None) -> str | None:
    """Extract Razorpay's ``error.code`` for logs, defensively -- never a crash."""
    if not body:
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _fetch_failure_code(status: int) -> RecoveryCode:
    """The shared classifier, corrected for a read.

    ``PAYMENT_FAILED`` from :func:`classify_failure` is the "definitely not performed"
    verdict for a mutation. A refused *read* performed nothing either, but that says
    nothing about the payment, and letting it through would let a 404 on a mistyped
    identifier mark a captured attempt ``FAILED``. Every other verdict is kept.
    """
    code = classify_failure(status)
    if code is RecoveryCode.PAYMENT_FAILED:
        return RecoveryCode.HUMAN_REVIEW_REQUIRED
    return code


# ------------------------------------------------------------------------------ fetches


def fetch_payment(
    transport: HttpTransport,
    config: RazorpayConfig,
    payment_id: str,
    *,
    expected_order_id: str,
    expected_amount: Money,
) -> PaymentFetchResult:
    """Read one payment and classify it. Sends exactly one request.

    Guarantees:

    * ``status`` ``captured`` yields ``CAPTURED`` evidence, ``authorized`` yields
      ``AUTHORIZED``, ``failed`` yields ``FAILED``, ``created`` and ``pending`` yield
      ``PENDING``; ``refunded`` yields ``CAPTURED`` with ``amount_refunded_minor`` set.
      Nothing else yields evidence;
    * evidence is produced only after the entity's ``id``, ``order_id``, ``amount`` and
      ``currency`` are checked against what the attempt recorded. A mismatch raises
      :class:`EvidenceMismatchError` and is never a state;
    * a transport failure, a 5xx, an unanticipated status, an unreadable body or an
      entity missing an identifying field is ``UNKNOWN`` with ``PAYMENT_UNKNOWN``;
    * a refused read (401/403, and 400/404/422) is ``UNKNOWN`` with
      ``HUMAN_REVIEW_REQUIRED``, because repeating the same read will repeat the refusal;
    * a readable entity with a status this adapter does not recognise is ``UNKNOWN`` with
      ``HUMAN_REVIEW_REQUIRED`` and the raw status in ``provider_error_code``, rather
      than a guess.

    Refuses to retry internally. Reconciliation is bounded by the caller (ADR 0003 D13).
    """
    request = build_fetch_payment_request(config, payment_id)

    try:
        response: HttpResponse = transport.send(request)
    except TransportError:
        return _unknown_payment(None)

    body = parse_json_body(response)
    if not response.is_success:
        return _unknown_payment(
            response.status, _fetch_failure_code(response.status), _provider_error_code(body)
        )
    if body is None:
        return _unknown_payment(response.status, error_code="unreadable_body")

    identity = _read_identity(body)
    if identity is None:
        return _unknown_payment(response.status, error_code="unreadable_entity")

    _check_echo(
        identity,
        expected_payment_id=payment_id,
        expected_order_id=expected_order_id,
        expected_amount=expected_amount,
    )

    classification = _PROVIDER_STATUS_CLASSIFICATION.get(identity.provider_status)
    if classification is None:
        return _unknown_payment(
            response.status,
            RecoveryCode.HUMAN_REVIEW_REQUIRED,
            f"unrecognised_status: {identity.provider_status}",
        )

    evidence = _evidence(
        body,
        identity,
        classification,
        raw_digest=sha256_hex(response.body),
        http_status=response.status,
    )
    return PaymentFetchResult(
        code=_CLASSIFICATION_CODE[classification],
        classification=classification,
        evidence=evidence,
        http_status=response.status,
    )


def fetch_order_payments(
    transport: HttpTransport,
    config: RazorpayConfig,
    order_id: str,
    *,
    expected_amount: Money,
) -> OrderPaymentsResult:
    """List every payment on an order and classify the order. Sends exactly one request.

    Guarantees, in addition to those of :func:`fetch_payment`, which apply to every item:

    * every item must echo ``order_id`` and the expected amount and currency, or the
      whole call raises :class:`EvidenceMismatchError` -- a foreign payment in the list
      means the list cannot be about this order;
    * the decisive payment is the one that progressed furthest (captured over authorized
      over pending over failed), so a failed first try never hides a captured retry;
    * an empty list is a verified ``PENDING`` with no evidence: the provider affirmatively
      said no payment has been attempted, which is information, not an unknown;
    * two or more payments in which money is held or taken is ``UNKNOWN`` with
      ``HUMAN_REVIEW_REQUIRED``. Razorpay does not let an order be paid twice, so seeing
      it means something is already wrong, and choosing one automatically would hide a
      double charge from the person who has to refund it.
    """
    request = build_order_payments_request(config, order_id)

    try:
        response: HttpResponse = transport.send(request)
    except TransportError:
        return _unknown_order(None)

    body = parse_json_body(response)
    if not response.is_success:
        return _unknown_order(
            response.status, _fetch_failure_code(response.status), _provider_error_code(body)
        )
    if body is None:
        return _unknown_order(response.status, error_code="unreadable_body")

    items = body.get("items")
    if not isinstance(items, list):
        return _unknown_order(response.status, error_code="unreadable_collection")

    raw_digest = sha256_hex(response.body)
    payments: list[ProviderEvidence] = []
    for item in items:
        if not isinstance(item, dict):
            return _unknown_order(response.status, error_code="unreadable_entity")
        identity = _read_identity(item)
        if identity is None:
            return _unknown_order(response.status, error_code="unreadable_entity")
        _check_echo(
            identity,
            expected_payment_id=None,
            expected_order_id=order_id,
            expected_amount=expected_amount,
        )
        classification = _PROVIDER_STATUS_CLASSIFICATION.get(identity.provider_status)
        if classification is None:
            return _unknown_order(
                response.status,
                RecoveryCode.HUMAN_REVIEW_REQUIRED,
                f"unrecognised_status: {identity.provider_status}",
            )
        payments.append(
            _evidence(
                item, identity, classification, raw_digest=raw_digest, http_status=response.status
            )
        )

    if not payments:
        return OrderPaymentsResult(
            code=RecoveryCode.PAYMENT_PENDING,
            classification=FetchClassification.PENDING,
            evidence=None,
            payments=(),
            http_status=response.status,
        )

    settled = [p for p in payments if p.classification in _MONEY_HELD]
    if len(settled) > 1:
        return OrderPaymentsResult(
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
            classification=FetchClassification.UNKNOWN,
            evidence=None,
            payments=tuple(payments),
            http_status=response.status,
            provider_error_code=f"multiple_settled_payments: {len(settled)}",
        )

    decisive = max(payments, key=lambda p: _DECISIVE_RANK[p.classification])
    return OrderPaymentsResult(
        code=_CLASSIFICATION_CODE[decisive.classification],
        classification=decisive.classification,
        evidence=decisive,
        payments=tuple(payments),
        http_status=response.status,
    )


def _unknown_payment(
    status: int | None,
    code: RecoveryCode = RecoveryCode.PAYMENT_UNKNOWN,
    error_code: str | None = None,
) -> PaymentFetchResult:
    return PaymentFetchResult(
        code=code,
        classification=FetchClassification.UNKNOWN,
        evidence=None,
        http_status=status,
        provider_error_code=error_code,
    )


def _unknown_order(
    status: int | None,
    code: RecoveryCode = RecoveryCode.PAYMENT_UNKNOWN,
    error_code: str | None = None,
) -> OrderPaymentsResult:
    return OrderPaymentsResult(
        code=code,
        classification=FetchClassification.UNKNOWN,
        evidence=None,
        payments=(),
        http_status=status,
        provider_error_code=error_code,
    )
