"""Signed AP2 receipts, and the rule that a payment receipt cannot be issued early.

Specification 15.2 asks for two kinds of receipt and draws a hard line between them:

    Checkout receipts for mandate verification outcomes.
    Payment receipts **only after verified payment capture**.

and specification 15.7 turns the second into a test it calls "no premature success receipt."

A checkout receipt says "I verified your mandates, and here is what I concluded." It is
issued as soon as verification finishes, and it is issued for rejections too -- a signed
rejection is more useful than silence, because it lets the other party prove what this
platform told them and when.

A payment receipt says "you have been paid." Issuing one before capture is verified is the
single worst thing this module could do. It is a signed statement, from a named issuer, that
money moved -- and the counterparty is entitled to act on it. If it turns out the capture
never happened, or failed, or is still unknown pending reconciliation, the platform has put
its signature on a false statement about money.

How that rule is enforced
-------------------------
Not by a comment and not by a runtime check inside the function. :func:`issue_payment_receipt`
cannot be called without a :class:`CaptureProof`, and :class:`CaptureProof` cannot be
constructed except through :func:`capture_proof`, which refuses any payment state that is not
``CAPTURED``. So "we might issue a receipt for an uncaptured payment" is not a bug that could
be introduced by a careless edit inside this module; it would require someone to first
manufacture a proof for a state the constructor rejects.

This mirrors how the rest of the platform handles the same class of problem -- the kernel's
Execution Grant, the agent runtime's missing approve method, the absent MCP payment tool.
The pattern is always the same: make the dangerous thing unconstructible rather than
guarded, because a guard is one edit away from being removed and a missing constructor is
not.

Why the SDK's own verifier is not used
---------------------------------------
``ap2.sdk.receipt_wrapper.ReceiptClient.verify_receipt`` at the pinned commit has three
defects that matter here. It annotates its key parameter as a ``cryptography`` public key
while passing it to a function that requires a ``jwcrypto`` JWK, so the documented usage
fails. It defaults ``has_reference_in_store_cb`` to ``None`` and then calls it
unconditionally, so the documented default crashes. And it checks only that the receipt's
reference is *known to the store*, never that it is the reference for the mandate being
settled -- which means it would happily verify a genuine receipt for somebody else's order.
It also swallows every failure into a dict rather than raising.

So the SDK's models are used for the receipt *shape*, which is what interoperability
depends on, and the verification is written here, where it can pin the algorithm, bind the
reference to the expected mandate, and refuse rather than return a dictionary that a caller
might not inspect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from ap2.sdk.generated.checkout_receipt import CheckoutReceipt
from ap2.sdk.generated.payment_receipt import PaymentReceipt
from commerce_domain import Money, canonicalize
from transaction_kernel import PaymentState

from ..core.errors import MandateRejected, SchemaRejected
from .signing import KeyRing, Signer, verify_compact

__all__ = [
    "RECEIPT_TYP",
    "CaptureProof",
    "capture_proof",
    "issue_checkout_receipt",
    "issue_payment_receipt",
    "issue_rejection_receipt",
    "verify_receipt",
]

#: The protected header ``typ`` on every receipt this platform signs.
RECEIPT_TYP: Final[str] = "JWT"


@dataclass(frozen=True, slots=True)
class CaptureProof:
    """Evidence that a payment actually captured. The key to issuing a payment receipt.

    Deliberately not constructible in the ordinary way: :func:`capture_proof` is the only
    function that builds one, and it refuses every payment state except ``CAPTURED``. The
    fields are the ones a receipt needs, so holding a proof and holding the facts a receipt
    asserts are the same thing.

    ``captured`` is :class:`~commerce_domain.Money`, integer minor units, because a receipt
    that names an amount must name the amount that actually moved and a float would let it
    name something a rounding away from it.
    """

    payment_attempt_id: str
    provider_payment_id: str
    captured: Money
    #: The kernel's own evidence source -- WEBHOOK or PROVIDER_FETCH. Recorded because
    #: ADR 0003 D8 is emphatic that a browser callback is never capture evidence, and a
    #: receipt should carry which authority it rests on.
    evidence_source: str


def capture_proof(
    *,
    payment_attempt_id: str,
    provider_payment_id: str,
    state: PaymentState,
    captured: Money,
    evidence_source: str,
) -> CaptureProof:
    """Build a :class:`CaptureProof`, or refuse because the payment did not capture.

    ``AUTHORIZED`` is refused as firmly as ``FAILED``, and that is the case worth being
    explicit about: an authorization is a hold, not a payment, and a receipt issued on one
    would assert that money moved when it has only been reserved. ``UNKNOWN`` and
    ``RECONCILING`` are refused for the opposite reason -- the outcome genuinely is not
    known yet, and a signature is not a way to resolve that.
    """
    if state is not PaymentState.CAPTURED:
        raise MandateRejected(
            "payment_receipt_requires_verified_capture",
            state=state.value,
            payment_attempt_id=payment_attempt_id,
        )
    if captured.minor <= 0:
        raise MandateRejected(
            "captured_amount_is_not_positive", payment_attempt_id=payment_attempt_id
        )
    if evidence_source not in {"WEBHOOK", "PROVIDER_FETCH"}:
        # ADR 0003 D8. A browser callback is a hint that a buyer came back, and the
        # platform records it as such; it is never the basis for saying a payment happened.
        raise MandateRejected(
            "capture_evidence_source_is_not_authoritative", source=evidence_source
        )
    return CaptureProof(
        payment_attempt_id=payment_attempt_id,
        provider_payment_id=provider_payment_id,
        captured=captured,
        evidence_source=evidence_source,
    )


def _sign(payload: dict[str, Any], signer: Signer) -> str:
    """Sign a receipt over its RFC 8785 canonical bytes.

    The same canonicalization the rest of the platform uses, so that a receipt's bytes are
    a function of its content alone. A counterparty that recomputes the payload from the
    fields it read gets back exactly what was signed.
    """
    return signer.sign({"typ": RECEIPT_TYP}, canonicalize(payload))


def issue_checkout_receipt(
    *,
    issuer: str,
    reference: str,
    order_id: str,
    signer: Signer,
    issued_at: int,
) -> str:
    """A signed success receipt for a verified checkout mandate.

    ``reference`` is the AP2 convention: ``base64url(SHA-256(closed mandate JWT))``. It is
    what lets the counterparty tie this receipt to the exact mandate it presented, so it is
    required rather than optional, and it is computed by the caller from the mandate it
    actually verified.

    ``issued_at`` is passed in rather than read from the process clock so that a receipt's
    timestamp can be the database transaction clock -- the same clock every other piece of
    this platform's evidence is stamped with. A receipt is evidence, and evidence stamped
    from a pod's own clock is evidence a skewed pod can misdate.
    """
    receipt = CheckoutReceipt(
        status="Success",
        iss=issuer,
        iat=issued_at,
        reference=reference,
        order_id=order_id,
    )
    return _sign(receipt.model_dump(mode="json", exclude_none=True), signer)


def issue_rejection_receipt(
    *,
    issuer: str,
    reference: str,
    error: str,
    error_description: str,
    signer: Signer,
    issued_at: int,
) -> str:
    """A signed rejection receipt, specification 15.2.

    Signing a refusal is not a courtesy. It lets the other party prove what this platform
    told them, which matters when the refusal is the reason a purchase did not complete and
    somebody later asks why. ``error`` is the stable key and ``error_description`` the
    sentence; neither may contain anything drawn from an unverified artifact, because a
    receipt is signed and a signature over attacker-chosen text is a signed statement of
    whatever the attacker chose.
    """
    receipt = CheckoutReceipt(
        status="Error",
        iss=issuer,
        iat=issued_at,
        reference=reference,
        error=error,
        error_description=error_description,
    )
    return _sign(receipt.model_dump(mode="json", exclude_none=True), signer)


def issue_payment_receipt(
    *,
    issuer: str,
    reference: str,
    proof: CaptureProof,
    signer: Signer,
    issued_at: int,
) -> str:
    """A signed payment receipt. Requires proof the payment captured.

    The proof is the first positional concern of the signature, and it is the whole design:
    there is no code path into this function that does not already hold evidence of capture,
    because the parameter cannot be satisfied any other way.

    ``psp_confirmation_id`` and ``network_confirmation_id`` both carry the provider payment
    id. The SDK's own helper mints a fresh UUID and uses it for all three fields, which
    would put an identifier in the receipt that corresponds to nothing anybody could look
    up. Naming the real provider identifier is more honest and more useful, and where this
    platform genuinely does not have a distinct network confirmation it says so by repeating
    what it does have rather than by inventing what it does not.
    """
    receipt = PaymentReceipt(
        status="Success",
        iss=issuer,
        iat=issued_at,
        reference=reference,
        payment_id=proof.provider_payment_id,
        psp_confirmation_id=proof.provider_payment_id,
        network_confirmation_id=proof.provider_payment_id,
    )
    return _sign(receipt.model_dump(mode="json", exclude_none=True), signer)


def verify_receipt(
    receipt_jwt: str,
    ring: KeyRing,
    *,
    expected_reference: str,
) -> dict[str, Any]:
    """Verify a receipt's signature and that it settles the mandate we expect.

    Two checks, and the second is the one the SDK omits. Verifying the signature proves the
    receipt is genuine; it does not prove the receipt is *ours*. A receipt correctly signed
    by the same issuer for a different order verifies perfectly and settles nothing here, so
    the reference is compared against the one the caller is trying to settle.

    Raises rather than returning a status dictionary. The SDK returns
    ``{'error': ...}`` on failure, which is a shape a caller can forget to inspect --
    and a forgotten check on a receipt is a payment treated as confirmed because nobody
    looked at the return value.
    """
    payload = verify_compact(receipt_jwt, ring)
    reference = payload.get("reference")
    if not isinstance(reference, str) or not reference:
        raise SchemaRejected("receipt_carries_no_reference")
    if reference != expected_reference:
        raise MandateRejected(
            "receipt_settles_a_different_mandate",
            expected=expected_reference,
            presented=reference,
        )
    status = payload.get("status")
    if status not in {"Success", "Error"}:
        raise SchemaRejected("receipt_status_not_recognised", status=str(status))
    return payload
