"""The gate between "a payment happened" and "give the buyer the goods".

Specification 11.1, 11.2 and 11.3. Two distinctions live here and both cost money when
they are collapsed.

**AUTHORIZED is not CAPTURED.** Auto-capture is enabled for the reference flow, which
makes the two states arrive milliseconds apart and makes it tempting to treat them as one
thing. They are not. An authorization is a hold; the money has not moved and may never
move. Fulfilling on an authorization ships goods against a payment that can still fail,
and -- worse, per specification 10.8 -- an authorization on a checkout that was
invalidated while the payment surface was open must be *released*, never captured.
Keeping them distinct is also what makes the no-regression rule meaningful:
``monotonic_apply`` can only refuse to rewind ``CAPTURED`` to ``AUTHORIZED`` if the two
are different states in the first place.

**A signed browser callback is not capture evidence.** Specification 11.2 is explicit
about it. The ``razorpay_signature`` returned to the browser proves that whoever produced
it holds the API key secret -- it does not prove the payment settled, and the browser is
not a channel the platform controls. A verified webhook or a server-side fetch of
provider state is evidence; a redirect is a hint that it is worth going to look.
"""

from __future__ import annotations

from enum import StrEnum

from transaction_kernel.states import PaymentState

__all__ = ["CaptureEvidence", "may_fulfil", "requires_release_not_capture"]


class CaptureEvidence(StrEnum):
    """How the platform came to believe a payment was captured.

    Ordered from least to most trustworthy in prose, though the code treats it as a set
    membership test rather than a ranking, so that adding a channel forces a decision
    about whether it is sufficient instead of inheriting one from its position.
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
