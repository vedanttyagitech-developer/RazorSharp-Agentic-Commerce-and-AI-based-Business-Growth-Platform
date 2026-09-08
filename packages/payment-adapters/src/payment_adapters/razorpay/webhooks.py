"""Webhook admission: verify, deduplicate, record, and only then let it change state.

Specification 11.3. Razorpay's own documentation says duplicate delivery is expected and
that ordering is not guaranteed, so this module treats both as normal traffic rather than
as errors. Nothing here rejects an event for being late or repeated; it simply refuses to
let one change the ledger twice or backwards.

Order of operations, and why it is this order
---------------------------------------------
1. **Verify the HMAC over the raw bytes.** Before anything else touches the payload.
2. **Derive the deduplication key.**
3. **Claim the key in the inbox.**
4. **Return.** Business processing happens afterwards, asynchronously.

Step 1 must precede step 3, and the reason is not tidiness. The endpoint is public. If an
unverified event could claim a deduplication key, anyone could POST a forged body
carrying the event id of a payment they expect to be delivered shortly; the key would be
taken, and the *genuine* signed webhook would arrive, be seen as a duplicate, and be
discarded. A capture would then never be applied. Verification first makes the inbox
claimable only by the holder of the webhook secret.

Deduplication keys
------------------
Razorpay sends ``x-razorpay-event-id`` on deliveries of the same event, so it is the
primary key. It is not guaranteed present -- older integrations and replayed deliveries
from the dashboard have been observed without it -- so there is a documented fallback
ladder, and every tier is **namespaced**:

===============  ==============================================  ============================
Tier             Key                                             When
===============  ==============================================  ============================
``evt:``         the ``x-razorpay-event-id`` header               header present and non-blank
``jcs:``         canonical (RFC 8785) hash of the parsed body     header absent, body is JSON
``raw:``         SHA-256 of the raw request bytes                 header absent, body is not
===============  ==============================================  ============================

The namespace prefixes are load-bearing. Without them a crafted event id of the form
``jcs:<hash>`` could collide with a fingerprint and suppress a real event; with them the
tiers occupy disjoint key spaces and no value from one can ever be mistaken for another.

The ``jcs:`` tier is preferred over hashing raw bytes because canonicalization is stable
across whitespace and key-order differences, so a redelivery that is semantically
identical but not byte-identical still deduplicates. It falls through to ``raw:`` when
the body is not JSON, or contains a float, which the platform's canonicalization profile
refuses -- a webhook is never dropped because its fingerprint could not be computed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from commerce_domain import (
    CanonicalizationError,
    Money,
    MoneyError,
    RecoveryCode,
    canonical_hash,
    sha256_b64url,
)
from transaction_kernel.states import PaymentState, monotonic_apply

from .config import RazorpayConfig
from .errors import UnmappableEventError
from .signatures import verify_webhook_signature
from .transport import parse_json_body_bytes

__all__ = [
    "EVENT_ID_HEADER",
    "SIGNATURE_HEADER",
    "InMemoryInboxStore",
    "InboxRecord",
    "InboxStore",
    "RazorpayWebhookEvent",
    "WebhookAdmission",
    "WebhookInbox",
    "apply_event",
    "dedup_key_for",
    "header_value",
    "parse_event",
    "payment_state_for_event",
]

EVENT_ID_HEADER: Final[str] = "x-razorpay-event-id"
SIGNATURE_HEADER: Final[str] = "x-razorpay-signature"


# --------------------------------------------------------------------------- headers


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup, returning ``None`` for absent or blank.

    HTTP header names are case-insensitive and every framework normalises them
    differently -- ``X-Razorpay-Event-Id``, ``x-razorpay-event-id``, ``HTTP_X_RAZORPAY_...``
    A case-sensitive lookup would silently fall through to the fingerprint tier on some
    deployments and use the header on others, so the same event could deduplicate under
    two different keys and be applied twice.

    A whitespace-only value is treated as absent, because a blank header is not an
    identifier and must not become one.
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            stripped = value.strip() if isinstance(value, str) else ""
            return stripped or None
    return None


# ------------------------------------------------------------------- deduplication key


def dedup_key_for(headers: Mapping[str, str], raw_body: bytes) -> str:
    """Derive the inbox key for this delivery. Deterministic, and never fails.

    Guarantees:

    * the same event delivered twice yields the same key, whether it is identified by
      header or by fingerprint;
    * keys from different tiers can never collide, because each tier is namespaced;
    * a body that cannot be parsed or canonicalized still yields a key, so no event is
      ever dropped for want of a fingerprint.

    See the module docstring for the tier table and the reasoning behind it.
    """
    event_id = header_value(headers, EVENT_ID_HEADER)
    if event_id is not None:
        return f"evt:{event_id}"

    parsed = parse_json_body_bytes(raw_body)
    if parsed is not None:
        try:
            return f"jcs:{canonical_hash(parsed)}"
        except CanonicalizationError:
            # The platform's canonicalization profile refuses floats (see
            # commerce_domain.jcs). A provider payload carrying one must still be
            # recorded, so we degrade to hashing the raw bytes rather than letting the
            # endpoint 500 and Razorpay retry the same event forever.
            pass
    return f"raw:{sha256_b64url(raw_body)}"


# ------------------------------------------------------------------------- event model


def _entity(body: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """Pull ``payload.<name>.entity`` defensively.

    Every level is checked because a malformed or partial payload must produce a recorded
    event with missing fields, never a ``TypeError`` that becomes a 500 and a retry loop.
    """
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return None
    wrapper = payload.get(name)
    if not isinstance(wrapper, dict):
        return None
    entity = wrapper.get("entity")
    return entity if isinstance(entity, dict) else None


def _money_from(entity: Mapping[str, Any] | None, field_name: str) -> Money | None:
    """Read an integer minor-unit field plus its currency into :class:`Money`.

    Refuses a float outright: Razorpay sends integer minor units, and a float here would
    mean either a mangled payload or a currency assumption we must not make silently.
    """
    if entity is None:
        return None
    amount = entity.get(field_name)
    currency = entity.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, int):
        return None
    if not isinstance(currency, str) or len(currency) != 3:
        return None
    try:
        return Money(amount, currency)
    except MoneyError:
        # A currency the platform does not accept is inbound data, not a crash. The event
        # is still recorded; it simply carries no amount the platform will reason about.
        return None


@dataclass(frozen=True, slots=True)
class RazorpayWebhookEvent:
    """A parsed webhook body. Every field may be absent; none of them is trusted.

    Parsing never raises. A malformed payload becomes an event with empty fields that is
    still recorded in the inbox, because the evidence that *something* arrived is worth
    more than a clean data model.
    """

    event_type: str
    account_id: str | None = None
    created_at: int | None = None
    payment_id: str | None = None
    order_id: str | None = None
    refund_id: str | None = None
    payment_status: str | None = None
    refund_status: str | None = None
    payment_amount: Money | None = None
    amount_refunded: Money | None = None
    refund_amount: Money | None = None

    @property
    def refund_covers_full_capture(self) -> bool | None:
        """Whether the payment is now fully refunded, or ``None`` if unknowable here.

        Computed from the payment entity Razorpay includes alongside a refund event:
        ``amount_refunded == amount`` means nothing is left to refund. Returning ``None``
        when the payment entity is absent is deliberate -- see
        :func:`payment_state_for_event`, which refuses to guess rather than defaulting.
        """
        if self.payment_amount is None or self.amount_refunded is None:
            return None
        if self.payment_amount.currency != self.amount_refunded.currency:
            return None
        # ``>=`` rather than ``==``: a provider total that somehow exceeds the capture
        # still means nothing is left to refund, and treating that as "partial" would
        # invite another refund against a payment that is already whole.
        return bool(self.amount_refunded >= self.payment_amount)

    @property
    def is_refund_event(self) -> bool:
        return self.event_type.startswith("refund.")


def parse_event(body: Mapping[str, Any]) -> RazorpayWebhookEvent:
    """Read a Razorpay webhook body into a value object. Never raises.

    Extracts the identifiers the platform needs to correlate the event with a local
    payment attempt, and the amounts needed to tell a full refund from a partial one.
    Anything absent or of the wrong type becomes ``None``.
    """
    event_type = body.get("event")
    payment = _entity(body, "payment")
    refund = _entity(body, "refund")
    order = _entity(body, "order")

    def _str(entity: Mapping[str, Any] | None, key: str) -> str | None:
        if entity is None:
            return None
        value = entity.get(key)
        return value if isinstance(value, str) and value else None

    created_at = body.get("created_at")
    account_id = body.get("account_id")

    return RazorpayWebhookEvent(
        event_type=event_type if isinstance(event_type, str) else "",
        account_id=account_id if isinstance(account_id, str) else None,
        created_at=created_at if isinstance(created_at, int) else None,
        payment_id=_str(payment, "id") or _str(refund, "payment_id"),
        order_id=_str(payment, "order_id") or _str(order, "id"),
        refund_id=_str(refund, "id"),
        payment_status=_str(payment, "status"),
        refund_status=_str(refund, "status"),
        payment_amount=_money_from(payment, "amount"),
        amount_refunded=_money_from(payment, "amount_refunded"),
        refund_amount=_money_from(refund, "amount"),
    )


# -------------------------------------------------------------------- event -> state


#: Razorpay event types whose local meaning is unambiguous.
#:
#: ``order.paid`` maps to ``CAPTURED`` because Razorpay emits it only once the order's
#: full amount is paid, which under auto-capture means captured.
#:
#: Absent on purpose: ``refund.processed``, which is ambiguous and is handled below;
#: ``refund.speed_changed`` and ``refund.arn_updated``, which carry no state change; and
#: every dispute event, which is a separate lifecycle this adapter does not model.
_EVENT_STATES: Final[Mapping[str, PaymentState]] = {
    "payment.pending": PaymentState.SUBMITTED,
    "payment.authorized": PaymentState.AUTHORIZED,
    "payment.captured": PaymentState.CAPTURED,
    "payment.failed": PaymentState.FAILED,
    "order.paid": PaymentState.CAPTURED,
    "refund.created": PaymentState.REFUND_PENDING,
    "refund.failed": PaymentState.REFUND_FAILED,
}

#: The one event whose meaning depends on an amount rather than on its name.
_AMBIGUOUS_REFUND_EVENT: Final[str] = "refund.processed"


def payment_state_for_event(
    event_type: str,
    *,
    refund_covers_full_capture: bool | None = None,
) -> PaymentState | None:
    """The state an event reports, or ``None`` when it reports no state change.

    Guarantees that an unrecognised event type yields ``None`` rather than a guess. New
    Razorpay events appear over time and an adapter that mapped them optimistically would
    move money-bearing state on a payload it has never seen.

    Refuses ``refund.processed`` without ``refund_covers_full_capture``. That event means
    ``REFUNDED`` for a full refund and ``PARTIALLY_REFUNDED`` for a partial one, and the
    difference is not recoverable from the event name. Guessing ``REFUNDED`` would put the
    attempt in a terminal state and permanently strand the remainder the buyer is still
    owed; guessing ``PARTIALLY_REFUNDED`` would leave a settled payment looking open
    forever. So it raises :class:`UnmappableEventError` and the caller supplies the fact.
    """
    if event_type == _AMBIGUOUS_REFUND_EVENT:
        if refund_covers_full_capture is None:
            raise UnmappableEventError(
                "refund.processed means REFUNDED for a full refund and PARTIALLY_REFUNDED "
                "for a partial one; supply refund_covers_full_capture rather than letting "
                "the adapter guess and strand the remainder"
            )
        return (
            PaymentState.REFUNDED if refund_covers_full_capture else PaymentState.PARTIALLY_REFUNDED
        )
    return _EVENT_STATES.get(event_type)


def apply_event(
    current: PaymentState,
    event: RazorpayWebhookEvent,
    *,
    refund_covers_full_capture: bool | None = None,
) -> PaymentState:
    """Fold one verified event into the attempt's state. Idempotent and monotonic.

    Guarantees, by delegating the join to ``transaction_kernel.states.monotonic_apply``:

    * ``CAPTURED`` never regresses to ``AUTHORIZED``. An ``payment.authorized`` event that
      arrives after ``payment.captured`` -- routine, since Razorpay does not guarantee
      ordering -- returns ``CAPTURED`` unchanged;
    * redelivery of the same event changes nothing;
    * a terminal attempt is never reopened by an inbound event;
    * an attempt in ``UNKNOWN`` or ``REFUND_UNKNOWN`` moves to ``RECONCILING``, never
      straight to the reported outcome, so a webhook can never substitute for a verified
      provider query.

    Events carrying no state meaning return ``current`` untouched.

    For ``refund.processed``, the full-versus-partial fact is taken from the event's own
    payment entity when Razorpay included one, and from the explicit argument otherwise.
    The explicit argument wins, so a caller holding the authoritative local ledger can
    override a payload that is stale or incomplete.

    Raises :class:`UnmappableEventError` for a ``refund.processed`` whose delivery
    carried no payment entity and for which no ``refund_covers_full_capture`` was given.
    This is the one event this function refuses to fold, and the caller must supply the
    fact from its own ledger rather than let a partial refund be recorded as a terminal
    ``REFUNDED``. It is safe to call from a worker: :meth:`WebhookInbox.admit` has
    already made the delivery durable, so the raise costs nothing but a retry.
    """
    resolved = refund_covers_full_capture
    if resolved is None:
        resolved = event.refund_covers_full_capture

    reported = payment_state_for_event(event.event_type, refund_covers_full_capture=resolved)
    if reported is None:
        return current
    return monotonic_apply(current, reported)


# ------------------------------------------------------------------------------ inbox


@dataclass(frozen=True, slots=True)
class InboxRecord:
    """The durable evidence that one delivery arrived, written before any processing.

    ``body_digest`` is kept so that two deliveries sharing a deduplication key but
    carrying different bytes are detectable after the fact. That is not supposed to
    happen; if it does, it means the provider reused an event id, and the platform should
    find out from its own records rather than from a buyer.
    """

    dedup_key: str
    event_type: str
    body_digest: str
    provider_event_id: str | None
    payment_id: str | None = None
    order_id: str | None = None
    refund_id: str | None = None


class InboxStore(Protocol):
    """Durable, single-winner claim on a deduplication key.

    The production implementation is one ``INSERT ... ON CONFLICT (tenant_id, dedup_key)
    DO NOTHING`` against a unique index, returning whether a row was written. That makes
    single-winner a database guarantee under concurrent delivery of the same event to two
    pods, rather than a check-then-write race that both sides win.

    ``claim`` must be atomic and must commit the record with -- never without -- the
    right to process the event.
    """

    def claim(self, record: InboxRecord) -> bool:
        """Store ``record`` if its key is unused. True when this caller may process it."""
        ...

    def get(self, dedup_key: str) -> InboxRecord | None:
        """Return the stored record for a key, or ``None``."""
        ...


class InMemoryInboxStore:
    """Reference :class:`InboxStore` for tests and the local demo. Not durable.

    Deliberately not thread-safe and deliberately not exported as production-ready: a
    process-local dict cannot deduplicate across pods, which is the case that matters.
    """

    def __init__(self) -> None:
        self._records: dict[str, InboxRecord] = {}

    def claim(self, record: InboxRecord) -> bool:
        if record.dedup_key in self._records:
            return False
        self._records[record.dedup_key] = record
        return True

    def get(self, dedup_key: str) -> InboxRecord | None:
        return self._records.get(dedup_key)

    def __len__(self) -> int:
        return len(self._records)


@dataclass(frozen=True, slots=True)
class WebhookAdmission:
    """The verdict on one delivery.

    ``accepted`` is true exactly once per event across every delivery of it, which is the
    property the caller relies on to make business processing run at most once.
    """

    accepted: bool
    code: RecoveryCode
    dedup_key: str | None
    event: RazorpayWebhookEvent | None

    @property
    def is_duplicate(self) -> bool:
        return self.code is RecoveryCode.DUPLICATE_OPERATION

    @property
    def should_acknowledge(self) -> bool:
        """Whether to answer the provider ``200`` immediately.

        True for a first delivery and for a duplicate: specification 11.3 asks for a quick
        success once receipt is durable, and re-delivering an event we already hold helps
        nobody. False for a signature failure, which must be rejected so that an attacker
        gets no confirmation that their forgery was stored.
        """
        return self.code in (RecoveryCode.OK, RecoveryCode.DUPLICATE_OPERATION)


class WebhookInbox:
    """Verify, deduplicate and durably record inbound Razorpay webhooks.

    Holds no state of its own; all state lives in the injected :class:`InboxStore`.
    """

    def __init__(self, store: InboxStore) -> None:
        self._store = store

    def admit(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
        config: RazorpayConfig,
    ) -> WebhookAdmission:
        """Admit one delivery. Verifies first, claims second, processes never.

        Guarantees:

        * an event whose HMAC does not verify against the **webhook secret** is refused
          with ``AUTHORITY_INSUFFICIENT`` and **nothing is written**. In particular it
          cannot claim a deduplication key, so a forged body carrying a real event id can
          never suppress the genuine delivery that follows;
        * the first verified delivery of an event returns ``accepted=True`` and ``OK``,
          with the record already durable in the store;
        * every later delivery of the same event returns ``accepted=False`` and
          ``DUPLICATE_OPERATION``, whatever order the deliveries arrive in;
        * the body is parsed only after verification, and parsing failures do not prevent
          the record from being stored.

        Performs no business processing and applies no state. Specification 11.3 requires
        that to happen asynchronously and idempotently after a quick acknowledgement, and
        keeping it out of this method is what makes the acknowledgement quick.
        """
        signature = header_value(headers, SIGNATURE_HEADER) or ""
        if not verify_webhook_signature(raw_body, signature, config.require_webhook_secret()):
            return WebhookAdmission(
                accepted=False,
                code=RecoveryCode.AUTHORITY_INSUFFICIENT,
                dedup_key=None,
                event=None,
            )

        # verify_webhook_signature accepts bytearray and memoryview as well as bytes --
        # ASGI and WSGI stacks hand back all three -- but json.loads refuses a memoryview
        # with a TypeError. Uncaught, that is a 500, and Razorpay retries the same event
        # forever against an endpoint that has already verified it. Normalise once, after
        # verification so that a str body still gets the explaining TypeError.
        body_bytes = bytes(raw_body)

        key = dedup_key_for(headers, body_bytes)
        parsed = parse_json_body_bytes(body_bytes)
        event = parse_event(parsed) if parsed is not None else RazorpayWebhookEvent(event_type="")

        record = InboxRecord(
            dedup_key=key,
            event_type=event.event_type,
            body_digest=sha256_b64url(body_bytes),
            provider_event_id=header_value(headers, EVENT_ID_HEADER),
            payment_id=event.payment_id,
            order_id=event.order_id,
            refund_id=event.refund_id,
        )

        if not self._store.claim(record):
            return WebhookAdmission(
                accepted=False,
                code=RecoveryCode.DUPLICATE_OPERATION,
                dedup_key=key,
                event=event,
            )

        return WebhookAdmission(
            accepted=True,
            code=RecoveryCode.OK,
            dedup_key=key,
            event=event,
        )
