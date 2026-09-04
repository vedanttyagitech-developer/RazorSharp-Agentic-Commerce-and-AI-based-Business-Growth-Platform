"""HMAC verification, specification 11.2 and 11.3.

The headline test in this file is
:func:`test_parsing_and_reserialising_the_body_breaks_the_signature`. It encodes the bug
that this module exists to prevent, and it fails the moment somebody "simplifies"
:func:`verify_webhook_signature` to accept a parsed body.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from payment_adapters.razorpay import (
    ConfigurationError,
    SignatureMismatchError,
    payment_signature_payload,
    require_payment_signature,
    require_webhook_signature,
    verify_payment_signature,
    verify_webhook_signature,
)
from payment_adapters.razorpay import signatures as signatures_module

from conftest import API_KEY_MATERIAL, WEBHOOK_KEY_MATERIAL, sign_body, sign_payment

ORDER_ID = "order_9A33XWu170gUtm"
PAYMENT_ID = "pay_29QQoUBi66xm2f"


# ------------------------------------------------------------- client-return signature


def test_a_genuine_client_return_verifies() -> None:
    signature = sign_payment(ORDER_ID, PAYMENT_ID, API_KEY_MATERIAL)
    assert verify_payment_signature(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        signature=signature,
        secret=API_KEY_MATERIAL,
    )


def test_the_signed_message_is_order_pipe_payment() -> None:
    """Pin the exact signing input. A change here invalidates every client return."""
    assert (
        payment_signature_payload(ORDER_ID, PAYMENT_ID)
        == b"order_9A33XWu170gUtm|pay_29QQoUBi66xm2f"
    )


def test_a_wrong_signature_is_refused() -> None:
    forged = hmac.new(b"not-the-secret", b"anything", hashlib.sha256).hexdigest()
    assert not verify_payment_signature(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        signature=forged,
        secret=API_KEY_MATERIAL,
    )


def test_a_signature_for_a_different_payment_is_refused() -> None:
    """The signature binds *both* identifiers, so one cannot be swapped for another."""
    signature = sign_payment(ORDER_ID, PAYMENT_ID, API_KEY_MATERIAL)
    assert not verify_payment_signature(
        order_id=ORDER_ID,
        payment_id="pay_someoneElsesPayment",
        signature=signature,
        secret=API_KEY_MATERIAL,
    )


def test_a_signature_made_with_the_webhook_secret_is_refused() -> None:
    """The two secrets are separate authorities and must not be interchangeable."""
    signature = sign_payment(ORDER_ID, PAYMENT_ID, WEBHOOK_KEY_MATERIAL)
    assert not verify_payment_signature(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        signature=signature,
        secret=API_KEY_MATERIAL,
    )


def test_uppercase_hex_is_accepted() -> None:
    """Comparison is on decoded digest bytes, so hex casing is not a rejection reason."""
    signature = sign_payment(ORDER_ID, PAYMENT_ID, API_KEY_MATERIAL)
    assert verify_payment_signature(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        signature=signature.upper(),
        secret=API_KEY_MATERIAL,
    )


@pytest.mark.parametrize(
    "signature",
    [
        "",
        "not-hex-at-all",
        "abcd",  # correct alphabet, wrong length
        "00" * 31,  # one byte short
        "00" * 33,  # one byte long
    ],
)
def test_malformed_signatures_are_refused_without_raising(signature: str) -> None:
    """A malformed signature and a wrong one take the same path: a flat ``False``.

    Distinguishing them would hand an attacker probing the endpoint a free oracle.
    """
    assert not verify_payment_signature(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        signature=signature,
        secret=API_KEY_MATERIAL,
    )


def test_an_empty_secret_raises_rather_than_verifying_everything() -> None:
    """With an empty key any attacker can compute a matching signature.

    Returning ``False`` would be survivable; returning ``True`` would be catastrophic, and
    an unset environment variable is exactly how the empty string arrives here. So the
    function refuses to compute at all.
    """
    with pytest.raises(ConfigurationError, match="empty secret"):
        verify_payment_signature(
            order_id=ORDER_ID, payment_id=PAYMENT_ID, signature="00" * 32, secret=""
        )


@pytest.mark.parametrize(
    ("order_id", "payment_id"),
    [
        ("order_9A33|XWu", PAYMENT_ID),
        (ORDER_ID, "pay_29QQ|oUBi"),
    ],
)
def test_an_identifier_containing_the_separator_is_refused(order_id: str, payment_id: str) -> None:
    """``a|b`` is ambiguous: two different identifier pairs can sign the same bytes.

    ``("order|x", "pay")`` and ``("order", "x|pay")`` produce identical signing input, so
    a signature valid for one would verify for the other.
    """
    with pytest.raises(ValueError, match="ambiguous"):
        payment_signature_payload(order_id, payment_id)


def test_empty_identifiers_are_refused() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        payment_signature_payload("", PAYMENT_ID)


# ------------------------------------------------------------------ webhook signature


def test_a_genuine_webhook_verifies_over_the_raw_bytes() -> None:
    raw = b'{"event":"payment.captured","payload":{}}'
    assert verify_webhook_signature(raw, sign_body(raw, WEBHOOK_KEY_MATERIAL), WEBHOOK_KEY_MATERIAL)


def test_parsing_and_reserialising_the_body_breaks_the_signature() -> None:
    """The bug this function guards, demonstrated end to end.

    A handler receives raw bytes, parses them, and re-serialises to verify. JSON is not
    canonical -- key order, whitespace and separators all survive the round trip
    differently than they arrived -- so the reconstructed bytes are a different message
    and the HMAC over them is a different digest.

    Verification against the *arrived* bytes succeeds; verification against the
    *reconstructed* bytes fails. If this test ever passes on both, the function has
    started normalising its input and no longer verifies what was actually signed.
    """
    raw = b'{"event": "payment.captured",  "created_at": 1767225600}'
    signature = sign_body(raw, WEBHOOK_KEY_MATERIAL)

    assert verify_webhook_signature(raw, signature, WEBHOOK_KEY_MATERIAL)

    reserialised = json.dumps(json.loads(raw)).encode("utf-8")
    assert reserialised != raw, "the fixture must actually change under a round trip"
    assert not verify_webhook_signature(reserialised, signature, WEBHOOK_KEY_MATERIAL)


def test_reordering_keys_breaks_the_signature() -> None:
    """The same JSON *meaning* is a different *message*. Only the bytes are signed."""
    raw = b'{"a":1,"b":2}'
    signature = sign_body(raw, WEBHOOK_KEY_MATERIAL)
    assert verify_webhook_signature(raw, signature, WEBHOOK_KEY_MATERIAL)
    assert not verify_webhook_signature(b'{"b":2,"a":1}', signature, WEBHOOK_KEY_MATERIAL)


def test_a_text_body_is_refused_with_a_type_error() -> None:
    """Accepting ``str`` would hide the re-serialisation bug behind a silent decode.

    A handler that passes text has almost certainly already parsed the body, and the
    resulting permanent verification failure is far harder to diagnose than this raise.
    """
    raw = b'{"event":"payment.captured"}'
    signature = sign_body(raw, WEBHOOK_KEY_MATERIAL)
    with pytest.raises(TypeError, match="raw request bytes"):
        verify_webhook_signature(raw.decode(), signature, WEBHOOK_KEY_MATERIAL)  # type: ignore[arg-type]


def test_a_signature_made_with_the_api_secret_is_not_a_valid_webhook_signature() -> None:
    """Specification 11.5: the webhook secret is separate authority, and stays separate."""
    raw = b'{"event":"payment.captured"}'
    assert not verify_webhook_signature(raw, sign_body(raw, API_KEY_MATERIAL), WEBHOOK_KEY_MATERIAL)


def test_a_tampered_body_is_refused() -> None:
    raw = b'{"event":"payment.captured","amount":39500}'
    signature = sign_body(raw, WEBHOOK_KEY_MATERIAL)
    tampered = raw.replace(b"39500", b"39501")
    assert not verify_webhook_signature(tampered, signature, WEBHOOK_KEY_MATERIAL)


def test_an_empty_body_still_verifies_against_its_own_signature() -> None:
    """An empty body is a legitimate message; only an empty *secret* is refused."""
    assert verify_webhook_signature(b"", sign_body(b"", WEBHOOK_KEY_MATERIAL), WEBHOOK_KEY_MATERIAL)


# -------------------------------------------------------------- constant-time comparison


def test_payment_verification_goes_through_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if anybody replaces ``hmac.compare_digest`` with ``==``.

    ``==`` on bytes short-circuits at the first differing byte, which on a public endpoint
    that accepts unlimited attempts is a byte-at-a-time signature recovery.
    """
    calls: list[tuple[object, object]] = []
    real = hmac.compare_digest

    def spy(a: object, b: object) -> bool:
        calls.append((a, b))
        return real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(signatures_module.hmac, "compare_digest", spy)
    signature = sign_payment(ORDER_ID, PAYMENT_ID, API_KEY_MATERIAL)
    assert verify_payment_signature(
        order_id=ORDER_ID, payment_id=PAYMENT_ID, signature=signature, secret=API_KEY_MATERIAL
    )
    assert calls, "verification must compare in constant time, never with =="


def test_webhook_verification_goes_through_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, object]] = []
    real = hmac.compare_digest

    def spy(a: object, b: object) -> bool:
        calls.append((a, b))
        return real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(signatures_module.hmac, "compare_digest", spy)
    raw = b'{"event":"payment.captured"}'
    assert verify_webhook_signature(raw, sign_body(raw, WEBHOOK_KEY_MATERIAL), WEBHOOK_KEY_MATERIAL)
    assert calls, "verification must compare in constant time, never with =="


# ------------------------------------------------------------------ require_* helpers


def test_require_helpers_raise_on_refusal() -> None:
    with pytest.raises(SignatureMismatchError):
        require_payment_signature(
            order_id=ORDER_ID, payment_id=PAYMENT_ID, signature="00" * 32, secret=API_KEY_MATERIAL
        )
    with pytest.raises(SignatureMismatchError):
        require_webhook_signature(b"{}", "00" * 32, WEBHOOK_KEY_MATERIAL)


def test_require_helpers_are_silent_on_success() -> None:
    signature = sign_payment(ORDER_ID, PAYMENT_ID, API_KEY_MATERIAL)
    require_payment_signature(
        order_id=ORDER_ID, payment_id=PAYMENT_ID, signature=signature, secret=API_KEY_MATERIAL
    )
    raw = b'{"event":"payment.captured"}'
    require_webhook_signature(raw, sign_body(raw, WEBHOOK_KEY_MATERIAL), WEBHOOK_KEY_MATERIAL)
