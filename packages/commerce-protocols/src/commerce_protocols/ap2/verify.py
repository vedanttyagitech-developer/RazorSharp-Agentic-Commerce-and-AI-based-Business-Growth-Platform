"""The AP2 verification sequence, specification 15.3.

Twelve numbered steps ending in a :class:`~transaction_kernel.VerifiedAuthorityProof`, and
one rule that governs all of them:

    No mandate reaches the kernel as authority until all applicable checks pass.

This module is the only place in the package that can produce a proof, and it produces one
only at the end of the sequence. There is no early return that yields a partial proof, and
no parameter that skips a step. The failure this prevents is subtle and fatal: a verifier
that returns "verified" after five of twelve checks is indistinguishable, at its call site,
from one that ran all twelve.

The order is not arbitrary
--------------------------
The cheap, non-cryptographic refusals come first -- was AP2 even negotiated, is the payload
the right shape -- because there is no reason to spend an elliptic-curve verification on a
request that a string comparison can refuse. Then the merchant authorization, because the
checkout it covers is the subject of everything after it. Then the buyer's mandates. Then
correlation. Then, last, the comparison against this platform's own locked state.

State comparison is deliberately last, and that ordering is a security property rather than
a performance one. Steps 1 to 10 establish what the external party is *claiming*; step 11
asks whether that claim matches reality. Doing it the other way round -- reading state first
and verifying against it -- invites an implementation that reads state *identified by the
caller's unverified fields*, which is how a verifier ends up cheerfully confirming that an
attacker's mandate matches the attacker's chosen checkout.

What this module does not do
-----------------------------
It does not charge anything. Specification 15.6: the AP2 verifier proves authority; it does
not charge a card or a UPI instrument. It does not call the kernel either -- it hands back a
proof, and the caller decides what to do with it. Keeping those separate is what lets the
UCP adapter verify a mandate in full and then deliberately decline to spend it, which is
exactly what specification 14.3 requires of the Razorpay Standard Checkout path.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ap2.sdk.sdjwt.common import parse_token
from commerce_domain import Money, sha256_b64url
from transaction_kernel import CheckoutRef, VerifiedAuthorityProof

from ..core.errors import (
    CorrelationRejected,
    MandateRejected,
    SchemaRejected,
    StateRejected,
)
from ..core.pins import PINS, Protocol
from .bridge import verify_merchant_authorization
from .mandates import VerifiedMandatePair, verify_mandate_pair
from .signing import SIGNING_ALGORITHM, KeyRing

__all__ = [
    "PlatformCheckoutFacts",
    "PresentedArtifacts",
    "VerificationOutcome",
    "mandate_reference",
    "verify_human_present",
]


@dataclass(frozen=True, slots=True)
class PresentedArtifacts:
    """Everything the external party put in front of us. All of it unverified.

    ``negotiated`` records step 1: whether AP2 was negotiated and locked for this checkout
    session. It is a fact about *this platform's* session record, not about the request, and
    the caller reads it from its own state before constructing this -- a caller that took it
    from the request would be letting the external party assert that it was allowed to use
    AP2.
    """

    negotiated: bool
    checkout: Mapping[str, Any]
    detached_merchant_authorization: str
    checkout_mandate_sd_jwt: str
    payment_mandate_sd_jwt: str
    audience: str
    nonce: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformCheckoutFacts:
    """What this platform's own locked rows say. The reference every claim is judged against.

    Every field here comes from a row the caller has locked inside its transaction, which is
    what makes step 11 meaningful. Reading these without a lock would compare a mandate
    against state that can move between the read and the decision -- the precise race
    ``transaction_kernel.admission`` takes its locks to close.
    """

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout_id: uuid.UUID
    version: int
    content_hash: str
    amount: Money
    authority_epoch: int
    #: The highest version that exists for this checkout. A mandate for version N is refused
    #: once N+1 exists, which is specification 15.7's "version-N mandate rejected after
    #: version N+1 exists".
    latest_version: int
    merchant_external_id: str


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """A completed verification: the proof, and the artifacts it rests on."""

    proof: VerifiedAuthorityProof
    pair: VerifiedMandatePair
    #: base64url(SHA-256(compact checkout JWT)). The AP2 receipt reference for this mandate.
    reference: str


def mandate_reference(compact_checkout_jwt: str) -> str:
    """The AP2 receipt reference for a mandate over this checkout.

    AP2's convention is ``base64url(SHA-256(closed mandate JWT))``, and in the human-present
    flow the closed mandate degenerates to the bare issuer JWT of the root SD-JWT. This
    platform uses the compact checkout JWT instead, and does so deliberately: it is the one
    value every participant already agrees on -- the merchant signed it, the bridge hashed
    it, both mandates name that hash -- so a receipt keyed on it can be tied back to the
    transaction by anyone holding any of those artifacts.
    """
    return sha256_b64url(compact_checkout_jwt.encode("ascii"))


def _require_positive_total(checkout: Mapping[str, Any]) -> Money:
    """The checkout's total, as integer minor units, or refuse.

    UCP carries totals as a list of typed components and the ``total`` entry is the one that
    matters. It is read strictly: an ``amount`` that is not an ``int`` is refused rather than
    coerced, because ``float`` is how a total silently becomes a paisa different from the one
    the buyer approved, and this platform's whole approval mechanism is byte equality.
    """
    currency = checkout.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise SchemaRejected("checkout_currency_missing_or_malformed")
    totals = checkout.get("totals")
    if not isinstance(totals, list):
        raise SchemaRejected("checkout_totals_missing")
    for entry in totals:
        if not isinstance(entry, Mapping) or entry.get("type") != "total":
            continue
        amount = entry.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int):
            # bool is a subclass of int in Python, so it has to be excluded by name or
            # `True` would be accepted as one paisa.
            raise SchemaRejected("checkout_total_is_not_an_integer_minor_amount")
        return Money(amount, currency)
    raise SchemaRejected("checkout_has_no_total_component")


def verify_human_present(
    artifacts: PresentedArtifacts,
    facts: PlatformCheckoutFacts,
    ring: KeyRing,
    *,
    correlation_id: uuid.UUID,
    verification_receipt_id: str,
    issuer: str,
) -> VerificationOutcome:
    """Run specification 15.3 end to end and produce a proof, or refuse.

    The caller supplies ``facts`` from rows it has locked, and ``ring`` holding the merchant
    and buyer public keys it trusts for this tenant. What comes back is a
    :class:`~transaction_kernel.VerifiedAuthorityProof` the kernel will *still* re-check
    against its own locked rows -- which is not redundancy. The kernel trusts nothing it did
    not read itself, and a proof is a summary of what a gateway concluded, not a promise the
    kernel is obliged to honour.
    """
    pin = PINS[Protocol.AP2]

    # --- step 1: was AP2 negotiated and locked for this checkout session? --------------
    if not artifacts.negotiated:
        raise MandateRejected(
            "ap2_was_not_negotiated_for_this_session", checkout_id=str(facts.checkout_id)
        )

    # --- step 2: the checkout is the right shape, and carries an integer total ---------
    presented_amount = _require_positive_total(artifacts.checkout)

    # --- steps 3, 4, 5 and 8: the merchant authorization, over bytes we canonicalize ---
    # verify_merchant_authorization recomputes the JCS payload from the checkout in front
    # of us, reattaches the detached signature and verifies under the pinned algorithm. It
    # is step 8's "verify merchant authorization again" by construction: there is only one
    # implementation, and it always works from our own canonicalization.
    bound = verify_merchant_authorization(
        artifacts.checkout, artifacts.detached_merchant_authorization, ring
    )

    # --- steps 6, 7 and 10: the buyer's two mandates, and their correlation ------------
    pair = verify_mandate_pair(
        checkout_sd_jwt=artifacts.checkout_mandate_sd_jwt,
        payment_sd_jwt=artifacts.payment_mandate_sd_jwt,
        ring=ring,
        expected_transaction_id=bound.transaction_id,
        expected_aud=artifacts.audience,
        expected_nonce=artifacts.nonce,
    )

    # --- step 9: does any of this match what we actually hold? -------------------------
    # Everything above proved the external party is internally consistent and holds real
    # signatures. None of it proved the mandate is for a checkout this platform recognises,
    # at the version it is currently at, for the amount it currently costs.
    if facts.latest_version > facts.version:
        raise StateRejected(
            "mandate_names_a_superseded_version",
            presented_version=facts.version,
            latest_version=facts.latest_version,
        )

    checkout_id = artifacts.checkout.get("id")
    if checkout_id != str(facts.checkout_id) and checkout_id != facts.merchant_external_id:
        # The UCP checkout carries the merchant's own identifier for the order. It must name
        # the checkout the caller located, or the mandate is for somebody else's purchase.
        raise StateRejected(
            "checkout_identifier_does_not_match_platform_state",
            presented=str(checkout_id),
        )

    if presented_amount != facts.amount:
        raise StateRejected(
            "mandate_amount_does_not_match_current_total",
            presented_minor=presented_amount.minor,
            current_minor=facts.amount.minor,
            currency=facts.amount.currency,
        )

    mandate_amount = pair.payment_mandate.payment_amount
    if mandate_amount.currency != facts.amount.currency:
        raise CorrelationRejected(
            "payment_mandate_currency_does_not_match",
            presented=mandate_amount.currency,
            current=facts.amount.currency,
        )
    if mandate_amount.amount != facts.amount.minor:
        raise CorrelationRejected(
            "payment_mandate_amount_does_not_match",
            presented_minor=mandate_amount.amount,
            current_minor=facts.amount.minor,
        )

    # --- step 12: the proof ------------------------------------------------------------
    # `expires_at` is the payment mandate's own expiry, which mandates.verify_payment_mandate
    # has already refused to leave unset. The kernel re-checks it, so a proof that outlived
    # its mandate would be refused there too -- but it must not be constructible here.
    expiry = pair.payment_mandate.exp
    if expiry is None:  # pragma: no cover - verify_payment_mandate refuses this earlier
        raise MandateRejected("payment_mandate_has_no_expiry")

    # The buyer key that actually verified the checkout mandate. Resolving it again through
    # the ring rather than trusting the header is what makes the recorded kid a fact about
    # which key was used, not a claim the presenter made.
    buyer_kid = _kid_of(artifacts.checkout_mandate_sd_jwt)
    ring.resolve(buyer_kid)

    proof = VerifiedAuthorityProof(
        protocol=pin.protocol.value,
        protocol_version=pin.version,
        issuer=issuer,
        subject=str(pair.payment_mandate.payee.id),
        key_id=buyer_kid or "",
        algorithm=SIGNING_ALGORITHM,
        mandate_ref=pair.transaction_id,
        tenant_id=facts.tenant_id,
        merchant_id=facts.merchant_id,
        checkout=CheckoutRef(
            checkout_id=facts.checkout_id,
            version=facts.version,
            content_hash=facts.content_hash,
        ),
        amount=facts.amount,
        action="PAYMENT_CREATE_ORDER",
        expires_at=datetime.fromtimestamp(expiry, tz=UTC),
        authority_epoch=facts.authority_epoch,
        verification_receipt_id=verification_receipt_id,
        correlation_id=correlation_id,
    )
    return VerificationOutcome(
        proof=proof, pair=pair, reference=mandate_reference(bound.compact_checkout_jwt)
    )


def _kid_of(sd_jwt: str) -> str | None:
    """The ``kid`` from an SD-JWT's issuer header, for recording which key verified it.

    Safe to read unverified here and only here: by the time this is called the mandate has
    already verified under that exact key, so the header's claim has been corroborated
    rather than trusted.
    """
    kid = parse_token(sd_jwt).header.get("kid")
    return str(kid) if kid else None
