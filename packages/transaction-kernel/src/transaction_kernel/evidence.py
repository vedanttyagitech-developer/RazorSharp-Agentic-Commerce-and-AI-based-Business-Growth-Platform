"""What counts as evidence that money moved, and what the kernel accepts as such.

ADR 0003 D2 and D8. Two things live here, and the reason they live in the kernel rather
than in the Razorpay adapter is dependency direction: the kernel decides whether a payment
may be fulfilled and whether a capture may be applied, so the vocabulary of that decision
must be importable without importing an adapter. ``payment_adapters.razorpay.fulfilment``
re-exports the first half of this module unchanged so existing imports keep working.

**The fulfilment gate** (``CaptureEvidence``, ``may_fulfil``,
``requires_release_not_capture``). Specification 11.1, 11.2 and 11.3. Two distinctions
live here and both cost money when they are collapsed.

*AUTHORIZED is not CAPTURED.* Auto-capture is enabled for the reference flow, which makes
the two states arrive milliseconds apart and makes it tempting to treat them as one thing.
They are not. An authorization is a hold; the money has not moved and may never move.
Fulfilling on an authorization ships goods against a payment that can still fail, and --
worse, per specification 10.8 -- an authorization on a checkout that was invalidated while
the payment surface was open must be *released*, never captured. Keeping them distinct is
also what makes the no-regression rule meaningful: ``monotonic_apply`` can only refuse to
rewind ``CAPTURED`` to ``AUTHORIZED`` if the two are different states in the first place.

*A signed browser callback is not capture evidence.* Specification 11.2 is explicit about
it. The ``razorpay_signature`` returned to the browser proves that whoever produced it
holds the API key secret -- it does not prove the payment settled, and the browser is not
a channel the platform controls. A verified webhook or a server-side fetch of provider
state is evidence; a redirect is a hint that it is worth going to look.

**The evidence record** (``EvidenceSource``, ``ProviderEvidence``). The shape in which a
worker hands the kernel what a provider said. It mirrors, field for field, the dataclass
``payment_adapters.razorpay.payments.ProviderEvidence`` produces, so the worker can pass
``dataclasses.asdict(result.evidence)`` to :meth:`ProviderEvidence.from_mapping` and the
kernel never imports the adapter. A cross-package test in payment-adapters proves the
mirror holds in both directions; a field added on either side fails that test rather than
silently becoming a value the kernel ignores.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from commerce_domain import DomainError

from transaction_kernel.states import PaymentState

__all__ = [
    "EVIDENCE_STATUSES",
    "CaptureEvidence",
    "EvidenceError",
    "EvidenceSource",
    "ProviderEvidence",
    "capture_channel",
    "may_fulfil",
    "requires_release_not_capture",
]


# ------------------------------------------------------------------- fulfilment gate


class CaptureEvidence(StrEnum):
    """How the platform came to believe a payment was captured.

    Ordered from least to most trustworthy in prose, though the code treats it as a set
    membership test rather than a ranking, so that adding a channel forces a decision
    about whether it is sufficient instead of inheriting one from its position.

    The values are compared by string from the adapter package (``EVIDENCE_SOURCE ==
    "PROVIDER_FETCH"``) and must therefore never change.
    """

    #: The buyer's browser posted ``razorpay_payment_id`` and ``razorpay_signature`` back
    #: to us. Signature-verified, and still never sufficient on its own.
    BROWSER_CALLBACK = "BROWSER_CALLBACK"

    #: A webhook whose HMAC over the raw body verified against the webhook secret.
    VERIFIED_WEBHOOK = "VERIFIED_WEBHOOK"

    #: A server-to-server fetch of the payment by authoritative identifier, which is what
    #: reconciliation performs (specification 10.7).
    PROVIDER_FETCH = "PROVIDER_FETCH"

    #: A human resolved the attempt through the operator path. Recorded as its own
    #: channel so that it is attributable in the audit stream rather than disguised as a
    #: provider fact.
    OPERATOR_RESOLUTION = "OPERATOR_RESOLUTION"


#: Channels that constitute verified capture evidence. ``BROWSER_CALLBACK`` is absent by
#: design; see the module docstring and specification 11.2 item 7.
_SUFFICIENT_EVIDENCE: frozenset[CaptureEvidence] = frozenset(
    {
        CaptureEvidence.VERIFIED_WEBHOOK,
        CaptureEvidence.PROVIDER_FETCH,
        CaptureEvidence.OPERATOR_RESOLUTION,
    }
)


def may_fulfil(state: PaymentState, evidence: CaptureEvidence) -> bool:
    """True only when the goods may be released against this payment.

    Guarantees both halves of the gate:

    * the attempt is exactly ``CAPTURED``. ``AUTHORIZED`` is refused because the money has
      not moved, and ``STALE_CAPTURE`` is refused because that capture belongs to a
      checkout version that was invalidated and is owed a full refund, not goods;
    * the evidence came from a server-side channel. A signature-verified browser callback
      alone is refused (specification 11.2).

    Refuses every other state, including the refund states: a payment that has entered a
    refund flow is not a payment that should be shipping anything new.
    """
    return state is PaymentState.CAPTURED and evidence in _SUFFICIENT_EVIDENCE


def requires_release_not_capture(state: PaymentState, *, checkout_invalidated: bool) -> bool:
    """True when an authorization must be released rather than captured.

    Specification 10.8: if the bound checkout version was invalidated while the payment
    surface was open and only an authorization exists, the platform does not intentionally
    capture it. Capturing would take money for a version that will never be fulfilled and
    would immediately owe a full refund.

    Answers only for ``AUTHORIZED``; a capture that already happened is a stale capture
    and is handled by the refund path, not by this question.
    """
    return checkout_invalidated and state is PaymentState.AUTHORIZED


# ----------------------------------------------------------------- evidence record


class EvidenceError(DomainError):
    """A mapping offered as provider evidence does not have the documented shape.

    Raised, never returned: an evidence record with a missing amount or an invented
    status is not something the kernel can apply "as best it can". The worker that built
    it has a bug, and the transaction it runs in must not commit a state change derived
    from it.
    """


class EvidenceSource(StrEnum):
    """The channel an evidence record came through.

    Distinct from :class:`CaptureEvidence` on purpose: this is the vocabulary a producer
    stamps on a record (the adapter writes ``"PROVIDER_FETCH"``, the webhook apply step
    writes ``"WEBHOOK"``, the client-return endpoint writes ``"BROWSER_CALLBACK"``), and
    :func:`capture_channel` is the one place that decides which fulfilment channel each
    source maps to. ``BROWSER_CALLBACK`` is a legal *source* so the callback can be
    recorded as evidence of what the browser said; it is never a sufficient *channel*.
    """

    WEBHOOK = "WEBHOOK"
    PROVIDER_FETCH = "PROVIDER_FETCH"
    BROWSER_CALLBACK = "BROWSER_CALLBACK"


#: Accepted spellings of a source on an inbound mapping. The adapter's ``CaptureEvidence``
#: value for a webhook is ``VERIFIED_WEBHOOK`` while the ADR's design text says
#: ``WEBHOOK``; both are accepted and normalised to the enum so a producer written against
#: either document works, and the stored value is always one string.
_SOURCE_ALIASES: Final[Mapping[str, EvidenceSource]] = {
    "WEBHOOK": EvidenceSource.WEBHOOK,
    "VERIFIED_WEBHOOK": EvidenceSource.WEBHOOK,
    "PROVIDER_FETCH": EvidenceSource.PROVIDER_FETCH,
    "BROWSER_CALLBACK": EvidenceSource.BROWSER_CALLBACK,
}

_CHANNEL_FOR_SOURCE: Final[Mapping[EvidenceSource, CaptureEvidence]] = {
    EvidenceSource.WEBHOOK: CaptureEvidence.VERIFIED_WEBHOOK,
    EvidenceSource.PROVIDER_FETCH: CaptureEvidence.PROVIDER_FETCH,
    EvidenceSource.BROWSER_CALLBACK: CaptureEvidence.BROWSER_CALLBACK,
}


def capture_channel(source: EvidenceSource) -> CaptureEvidence:
    """The fulfilment channel a source counts as, for :func:`may_fulfil`."""
    return _CHANNEL_FOR_SOURCE[source]


#: The classifications a producer may report. Mirrors the adapter's ``FetchClassification``
#: minus ``unknown``: an unknown fetch produces no evidence object at all, so a record that
#: says "unknown" is a producer bug, not a fact about the money.
EVIDENCE_STATUSES: Final[frozenset[str]] = frozenset(
    {"authorized", "captured", "failed", "pending"}
)

#: The attempt state each classification supports. ``pending`` maps to nothing: a payment
#: the buyer has not finished is not a transition, and the attempt stays where it is.
_STATE_FOR_STATUS: Final[Mapping[str, PaymentState]] = {
    "captured": PaymentState.CAPTURED,
    "authorized": PaymentState.AUTHORIZED,
    "failed": PaymentState.FAILED,
}

_HEX_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")

#: Field-width limits that match the columns the values are eventually stored beside
#: (``payment_attempts.provider_*`` are ``String(64)``); refused here so a malformed
#: identifier is a legible error rather than a truncation or a failed INSERT.
_MAX_IDENTIFIER: Final = 64
_MAX_TEXT: Final = 128


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    """One payment as a provider reported it, in provider-neutral primitives.

    Built through :meth:`from_mapping` from the adapter's dataclass (or from a webhook
    payload the worker has already verified), and validated on construction so that every
    instance the kernel holds is well-formed. The fields mirror
    ``payment_adapters.razorpay.payments.ProviderEvidence`` exactly, plus ``event_id``:

    ``source``
        Which channel produced the record. Capture is applied only from ``WEBHOOK`` or
        ``PROVIDER_FETCH`` (ADR D8); a ``BROWSER_CALLBACK`` record is refused by
        ``payments.apply_provider_evidence`` and belongs to ``record_browser_callback``.
    ``provider_payment_id`` / ``provider_order_id``
        The provider's identifiers exactly as echoed. Cross-checked against the attempt
        before anything is applied.
    ``amount_minor`` / ``currency``
        Integer minor units and ISO code. The adapter has already verified these against
        the attempt; the kernel verifies them again, because the kernel is the component
        whose guarantee it is.
    ``status``
        One of ``authorized``, ``captured``, ``failed``, ``pending``.
    ``provider_status``
        The raw provider status string, kept so a reviewer can see why ``status`` says
        what it says -- in particular that a ``refunded`` payment arrives as ``captured``.
    ``created_at`` / ``captured_at`` / ``authorized_at``
        RFC 3339 UTC timestamps (``2026-01-01T00:00:00Z``) or ``None``. A producer may
        supply Unix epoch seconds as the adapter does; they are normalised here and never
        synthesised from a local clock.
    ``amount_refunded_minor``
        The provider's refunded total when present. Non-zero on a capture means the refund
        ledger must be reconciled too.
    ``method``, ``error_code``, ``error_reason``, ``http_status``
        Descriptive fields for the inspector document and the audit row.
    ``raw_digest``
        Lowercase hex SHA-256 of the raw bytes the evidence was read from.
    ``event_id``
        The provider's webhook event id for ``WEBHOOK`` evidence; ``None`` for a fetch.
    """

    source: EvidenceSource
    provider_payment_id: str
    provider_order_id: str
    amount_minor: int
    currency: str
    status: str
    provider_status: str
    raw_digest: str
    created_at: str | None = None
    captured_at: str | None = None
    authorized_at: str | None = None
    amount_refunded_minor: int | None = None
    method: str | None = None
    error_code: str | None = None
    error_reason: str | None = None
    http_status: int | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise EvidenceError(f"source must be an EvidenceSource, got {self.source!r}")
        _require_identifier(self.provider_payment_id, "provider_payment_id")
        _require_identifier(self.provider_order_id, "provider_order_id")
        _require_int(self.amount_minor, "amount_minor", minimum=1)
        if not isinstance(self.currency, str) or not _CURRENCY.match(self.currency):
            raise EvidenceError(
                f"currency must be a 3-letter uppercase code, got {self.currency!r}"
            )
        if self.status not in EVIDENCE_STATUSES:
            raise EvidenceError(
                f"status must be one of {sorted(EVIDENCE_STATUSES)}, got {self.status!r}; an "
                "unknown fetch produces no evidence at all"
            )
        _require_text(self.provider_status, "provider_status", limit=_MAX_IDENTIFIER)
        if not isinstance(self.raw_digest, str) or not _HEX_DIGEST.match(self.raw_digest):
            raise EvidenceError("raw_digest must be lowercase hex SHA-256 of the raw body")
        for name in ("created_at", "captured_at", "authorized_at"):
            value = getattr(self, name)
            if value is not None:
                _require_rfc3339(value, name)
        if self.amount_refunded_minor is not None:
            _require_int(self.amount_refunded_minor, "amount_refunded_minor", minimum=0)
        for name in ("method", "error_code", "error_reason"):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name, limit=_MAX_TEXT)
        if self.http_status is not None:
            _require_int(self.http_status, "http_status", minimum=100)
            if self.http_status > 599:
                raise EvidenceError(f"http_status {self.http_status} is not an HTTP status")
        if self.event_id is not None:
            _require_text(self.event_id, "event_id", limit=_MAX_IDENTIFIER)
        if self.source is EvidenceSource.PROVIDER_FETCH and self.event_id is not None:
            raise EvidenceError(
                "a PROVIDER_FETCH record carries no event_id; fetches are not events"
            )

    # ---- derived facts ------------------------------------------------------------

    @property
    def payment_state(self) -> PaymentState | None:
        """The attempt state this evidence supports, or ``None`` for a pending payment."""
        return _STATE_FOR_STATUS.get(self.status)

    @property
    def channel(self) -> CaptureEvidence:
        """The fulfilment channel this record counts as."""
        return capture_channel(self.source)

    @property
    def money_held(self) -> bool:
        """True when the provider is holding or has taken the buyer's money."""
        return self.status in ("authorized", "captured")

    @property
    def refund_reported(self) -> bool:
        """True when the provider says some of this capture has already gone back."""
        return self.provider_status == "refunded" or bool(self.amount_refunded_minor)

    def as_record(self) -> dict[str, Any]:
        """The evidence as JSON-safe primitives, for ``orders.capture_evidence`` and audit.

        Every value is a ``str``, ``int`` or ``None``; ``source`` is stored by value so the
        column never depends on this enum's Python identity.
        """
        return {
            "source": self.source.value,
            "channel": self.channel.value,
            "provider_payment_id": self.provider_payment_id,
            "provider_order_id": self.provider_order_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "status": self.status,
            "provider_status": self.provider_status,
            "created_at": self.created_at,
            "captured_at": self.captured_at,
            "authorized_at": self.authorized_at,
            "amount_refunded_minor": self.amount_refunded_minor,
            "method": self.method,
            "error_code": self.error_code,
            "error_reason": self.error_reason,
            "http_status": self.http_status,
            "raw_digest": self.raw_digest,
            "event_id": self.event_id,
        }

    # ---- construction from a producer's mapping ------------------------------------

    @classmethod
    def field_names(cls) -> frozenset[str]:
        """The closed vocabulary :meth:`from_mapping` accepts."""
        return frozenset(f.name for f in fields(cls))

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> ProviderEvidence:
        """Build evidence from a plain mapping, strictly.

        Accepts exactly the keys of this dataclass -- which are the adapter's fields plus
        ``event_id`` -- so ``dataclasses.asdict(adapter_evidence)`` is accepted verbatim.
        Refuses, with :class:`EvidenceError`:

        * an unknown key. A producer that adds a field must decide, in this module, what
          the kernel does with it; a silently dropped field is how a fact gets lost;
        * a missing required key (source, identifiers, amount, currency, status,
          provider_status, raw_digest);
        * a wrong type anywhere, including ``bool`` where an ``int`` is expected;
        * a source outside ``WEBHOOK`` / ``VERIFIED_WEBHOOK`` / ``PROVIDER_FETCH`` /
          ``BROWSER_CALLBACK``.

        Timestamps may be Unix epoch seconds (as the adapter emits) or RFC 3339 strings;
        both are stored as RFC 3339 UTC.
        """
        if not isinstance(mapping, Mapping):
            raise EvidenceError(f"evidence must be a mapping, got {type(mapping).__name__}")
        allowed = cls.field_names()
        unknown = sorted(set(mapping) - allowed)
        if unknown:
            raise EvidenceError(
                f"evidence carries unknown fields {unknown}; the kernel mirror must be updated "
                "before a producer may send them"
            )
        required = (
            "source",
            "provider_payment_id",
            "provider_order_id",
            "amount_minor",
            "currency",
            "status",
            "provider_status",
            "raw_digest",
        )
        missing = [name for name in required if name not in mapping]
        if missing:
            raise EvidenceError(f"evidence is missing required fields {missing}")

        raw_source = mapping["source"]
        if not isinstance(raw_source, str) or raw_source not in _SOURCE_ALIASES:
            raise EvidenceError(
                f"source must be one of {sorted(_SOURCE_ALIASES)}, got {raw_source!r}"
            )

        return cls(
            source=_SOURCE_ALIASES[raw_source],
            provider_payment_id=mapping["provider_payment_id"],
            provider_order_id=mapping["provider_order_id"],
            amount_minor=mapping["amount_minor"],
            currency=mapping["currency"],
            status=mapping["status"],
            provider_status=mapping["provider_status"],
            raw_digest=mapping["raw_digest"],
            created_at=_timestamp(mapping.get("created_at"), "created_at"),
            captured_at=_timestamp(mapping.get("captured_at"), "captured_at"),
            authorized_at=_timestamp(mapping.get("authorized_at"), "authorized_at"),
            amount_refunded_minor=mapping.get("amount_refunded_minor"),
            method=mapping.get("method"),
            error_code=mapping.get("error_code"),
            error_reason=mapping.get("error_reason"),
            http_status=mapping.get("http_status"),
            event_id=mapping.get("event_id"),
        )


# ------------------------------------------------------------------------ validators


def _require_int(value: object, name: str, *, minimum: int) -> None:
    """An ``int`` that is not a ``bool`` (which ``isinstance`` would let through)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise EvidenceError(f"{name} must be >= {minimum}, got {value}")


def _require_text(value: object, name: str, *, limit: int) -> None:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{name} must be a non-empty string, got {value!r}")
    if len(value) > limit:
        raise EvidenceError(f"{name} is {len(value)} characters; the limit is {limit}")


def _require_identifier(value: object, name: str) -> None:
    """A provider identifier: non-empty, no whitespace, fits the column."""
    _require_text(value, name, limit=_MAX_IDENTIFIER)
    assert isinstance(value, str)
    if any(ch.isspace() for ch in value):
        raise EvidenceError(f"{name} {value!r} contains whitespace; no provider identifier does")


def _require_rfc3339(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise EvidenceError(
            f"{name} must be an RFC 3339 string or None, got {type(value).__name__}"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{name} {value!r} is not an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{name} {value!r} has no UTC offset; a naive timestamp is ambiguous")


def _timestamp(value: object, name: str) -> str | None:
    """Normalise a producer's timestamp to RFC 3339 UTC, or refuse it.

    Epoch seconds are what Razorpay emits and what the adapter passes through unchanged;
    they are converted with an explicit UTC zone so the result does not depend on the
    worker's locale. A string is accepted only if it already parses as RFC 3339 with an
    offset, and is re-rendered so the stored form is uniform.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise EvidenceError(f"{name} must be epoch seconds or RFC 3339, got a bool")
    if isinstance(value, int):
        if value < 0:
            raise EvidenceError(f"{name} epoch seconds must not be negative, got {value}")
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        _require_rfc3339(value, name)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise EvidenceError(f"{name} must be epoch seconds or RFC 3339, got {type(value).__name__}")
