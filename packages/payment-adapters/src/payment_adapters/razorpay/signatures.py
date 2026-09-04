"""HMAC verification for Razorpay client returns and webhooks, specification 11.2/11.3.

Two signatures, two secrets, two different signing inputs, and one shared discipline:
compare with :func:`hmac.compare_digest` and never with ``==``.

``==`` on bytes short-circuits at the first differing byte. An attacker who can submit
many candidate signatures and measure the response time can recover a valid signature
one byte at a time. That is not a theoretical attack on a public endpoint that a payment
provider is expected to call from anywhere; it is the reason the constant-time primitive
exists in the standard library.

The second discipline is about *what* is signed:

* the client return signs ``"{order_id}|{payment_id}"`` with the **API key secret**;
* the webhook signs the **raw request body bytes** with the **webhook secret**.

The webhook case is where implementations go wrong. A framework hands the handler a
parsed dict, the handler re-serialises it to check the signature, and verification fails
forever -- or worse, the handler "fixes" it by skipping verification. JSON is not
canonical: key order, whitespace, unicode escaping and integer formatting all survive a
round trip differently than they arrived. The bytes that were signed are the bytes that
arrived, and nothing else. :func:`verify_webhook_signature` therefore refuses ``str``
outright rather than encoding it and hoping.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

from .errors import ConfigurationError, SignatureMismatchError

__all__ = [
    "PAYMENT_SIGNATURE_SEPARATOR",
    "payment_signature_payload",
    "require_payment_signature",
    "require_webhook_signature",
    "verify_payment_signature",
    "verify_webhook_signature",
]

#: Razorpay's fixed separator for the client-return signing string.
PAYMENT_SIGNATURE_SEPARATOR: Final[str] = "|"

#: Length in bytes of a SHA-256 digest. A candidate signature that does not decode to
#: exactly this is rejected before any comparison.
_DIGEST_BYTES: Final[int] = 32


def _hmac_sha256(secret: str, message: bytes) -> bytes:
    """HMAC-SHA256 digest bytes, refusing an empty key.

    An empty HMAC key is not a weak key, it is a *shared* key: anyone can compute a
    matching signature with it. A configuration path that yields ``""`` -- an unset
    environment variable, a secret manager returning a blank -- would otherwise turn
    verification into a rubber stamp that reports success, which is the worst possible
    failure mode for this function.
    """
    if not secret:
        raise ConfigurationError(
            "refusing to compute an HMAC with an empty secret: every candidate signature "
            "would verify against it"
        )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def _decode_hex_signature(signature: str) -> bytes | None:
    """Decode a hex signature to digest bytes, or ``None`` if it is not one.

    Comparison happens on decoded bytes rather than on hex text so that a correct
    signature in uppercase hex is accepted, and so that a candidate of the wrong length
    is rejected structurally instead of relying on the comparison to notice.

    Returning ``None`` rather than raising keeps a malformed signature and a wrong
    signature on the same code path: a caller must not be able to distinguish "your hex
    was invalid" from "your HMAC was wrong", because that distinction is a free oracle.
    """
    if not signature:
        return None
    try:
        raw = bytes.fromhex(signature.strip())
    except ValueError:
        return None
    if len(raw) != _DIGEST_BYTES:
        return None
    return raw


def payment_signature_payload(order_id: str, payment_id: str) -> bytes:
    """Build the exact bytes Razorpay signs for a client return.

    Refuses an identifier containing the separator. Razorpay identifiers never contain
    ``|``, but the concatenation ``a|b`` is ambiguous in principle: the pair
    ``("order|x", "pay")`` and the pair ``("order", "x|pay")`` produce identical signing
    input, so a signature valid for one would verify for the other. Rejecting the
    separator removes the ambiguity instead of assuming the provider will never permit it.

    Also refuses an empty identifier, which would make the signing input depend on only
    one of the two values.
    """
    for label, value in (("order_id", order_id), ("payment_id", payment_id)):
        if not value:
            raise ValueError(f"{label} must not be empty")
        if PAYMENT_SIGNATURE_SEPARATOR in value:
            raise ValueError(
                f"{label} must not contain {PAYMENT_SIGNATURE_SEPARATOR!r}: it would make "
                "the signing input ambiguous between two different identifier pairs"
            )
    return f"{order_id}{PAYMENT_SIGNATURE_SEPARATOR}{payment_id}".encode()


def verify_payment_signature(
    *,
    order_id: str,
    payment_id: str,
    signature: str,
    secret: str,
) -> bool:
    """Verify the ``razorpay_signature`` returned to the browser after checkout.

    Guarantees a constant-time comparison against HMAC-SHA256 of
    ``"{order_id}|{payment_id}"`` keyed by the **API key secret**.

    Refuses -- returns ``False`` -- for a wrong signature, a signature that is not
    64 hex characters, and an empty signature. Raises ``ConfigurationError`` for an
    empty secret and ``ValueError`` for an identifier that would make the signing input
    ambiguous, because both of those are faults in this process rather than in the
    request.

    A ``True`` result proves only that whoever produced the signature holds the API key
    secret. Specification 11.2 requires more before money is treated as captured: the
    session must own the attempt, the request must be idempotent, and provider state must
    be fetched. The browser callback alone is never capture evidence -- see
    :func:`payment_adapters.razorpay.fulfilment.may_fulfil`.
    """
    candidate = _decode_hex_signature(signature)
    expected = _hmac_sha256(secret, payment_signature_payload(order_id, payment_id))
    if candidate is None:
        # Still burn the HMAC above so that a malformed signature and a wrong one cost
        # the same work. Then refuse.
        return False
    return hmac.compare_digest(expected, candidate)


def verify_webhook_signature(
    raw_body: bytes,
    signature: str,
    webhook_secret: str,
) -> bool:
    """Verify ``x-razorpay-signature`` over the **raw request body bytes**.

    Guarantees a constant-time comparison against HMAC-SHA256 of exactly the bytes
    passed in, keyed by the **webhook secret**, which specification 11.5 requires to be
    distinct from the API key secret.

    Refuses ``str``. Passing text means the body has already been decoded and, in almost
    every real handler, parsed and re-serialised. A re-serialised body is a different
    sequence of bytes -- ``{"a":1, "b":2}`` and ``{"b":2,"a":1}`` carry the same meaning
    and different signatures -- so verification would fail for a genuine event. Raising
    ``TypeError`` surfaces that at the call site instead of producing a mysterious
    permanent rejection.

    Returns ``False`` for a wrong or malformed signature. Raises ``ConfigurationError``
    for an empty webhook secret.
    """
    if not isinstance(raw_body, (bytes, bytearray, memoryview)):
        raise TypeError(
            "raw_body must be the raw request bytes, not text: a parsed and re-serialised "
            "body is a different byte sequence and will never match the signature"
        )
    body = bytes(raw_body)
    candidate = _decode_hex_signature(signature)
    expected = _hmac_sha256(webhook_secret, body)
    if candidate is None:
        return False
    return hmac.compare_digest(expected, candidate)


def require_payment_signature(
    *,
    order_id: str,
    payment_id: str,
    signature: str,
    secret: str,
) -> None:
    """Verify a client return, raising ``SignatureMismatchError`` on refusal.

    For call sites that must not be able to forget the ``if``. Behaviour is otherwise
    identical to :func:`verify_payment_signature`.
    """
    if not verify_payment_signature(
        order_id=order_id, payment_id=payment_id, signature=signature, secret=secret
    ):
        raise SignatureMismatchError("razorpay_signature did not verify")


def require_webhook_signature(
    raw_body: bytes,
    signature: str,
    webhook_secret: str,
) -> None:
    """Verify a webhook, raising ``SignatureMismatchError`` on refusal.

    Behaviour is otherwise identical to :func:`verify_webhook_signature`.
    """
    if not verify_webhook_signature(raw_body, signature, webhook_secret):
        raise SignatureMismatchError("x-razorpay-signature did not verify")
