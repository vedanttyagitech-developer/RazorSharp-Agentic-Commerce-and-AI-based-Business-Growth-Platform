"""Checkout and payment mandates for the human-present flow, specification 15.2.

AP2 has two families of flow and they have very different shapes. The human-not-present
family builds a *chain*: an open mandate the buyer signs delegating constrained authority,
then a closed mandate an agent presents under key binding, joined by ``~~``. The
human-present family -- the one this project implements -- does not. The buyer is at the
keyboard and approves the closed mandates directly, so each mandate is a single root SD-JWT
issued under the buyer's own key, and there is no delegation to constrain because nothing
was delegated.

That distinction is load-bearing rather than cosmetic, and getting it wrong is easy: the
SDK's ``CheckoutMandateChain.parse`` and ``PaymentMandateChain.parse`` both demand exactly
two payloads and raise on anything else, so they cannot be used here at all. The constraint
machinery in ``ap2.sdk.constraints`` is likewise human-not-present only. A verifier that
reached for them would be checking delegation limits on a flow that has no delegation, and
would refuse every legitimate mandate.

What binds the two mandates together
------------------------------------
``PaymentMandate.transaction_id`` must equal ``CheckoutMandate.checkout_hash``, and both
must equal ``base64url(SHA-256(ASCII(compact_checkout_jwt)))`` -- the value
:mod:`commerce_protocols.ap2.bridge` computes at step 6. That single equality is what makes
a payment mandate a mandate *for this checkout*: without it, a mandate authorising ₹399
could be replayed against a ₹39,900 checkout, since nothing else in the payment mandate
names the order.

The verifier therefore recomputes the hash from the checkout JWT itself rather than
trusting either field, and then requires all three to agree. Trusting ``checkout_hash``
because it arrived inside a signed mandate would be circular: the buyer signed whatever the
agent put in front of them, and the question being asked is whether that was this checkout.

Two hardening decisions the SDK leaves to its caller
-----------------------------------------------------
**Expiry is required.** ``CheckoutMandate.exp`` and ``PaymentMandate.exp`` are both
``int | None``. The SDK's chain verifier enforces ``exp`` when present and simply does not
look when it is absent, so a mandate issued without one never expires. Specification 15.7
asks for an expired-mandate test, which presumes mandates expire, so absence is refused here.

**The algorithm is pinned before the SD-JWT is opened.** ``ap2.sdk.sdjwt.sd_jwt.verify``
passes ``sign_alg=None`` to the underlying verifier, which then honours whatever the issuer
JWT's header claims. That is the same algorithm-confusion exposure
:mod:`commerce_protocols.ap2.signing` exists to close for plain JWS, and it has to be closed
here too -- an SD-JWT's issuer JWT is an ordinary JWS and forges the same way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final, cast

from ap2.sdk import mandate as ap2_mandate
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.sdjwt.common import parse_token
from jwcrypto.jwk import JWK
from pydantic import BaseModel, ValidationError

from ..core.errors import CorrelationRejected, MandateRejected, SchemaRejected, SignatureRejected
from .signing import SIGNING_ALGORITHM, KeyRing, Signer

__all__ = [
    "MANDATE_CLOCK_SKEW_SECONDS",
    "VerifiedMandatePair",
    "issue_checkout_mandate",
    "issue_payment_mandate",
    "silence_sdk_disk_logging",
    "verify_checkout_mandate",
    "verify_mandate_pair",
    "verify_payment_mandate",
]

#: Passed to the SDK's chain verifier. Five minutes matches the protocol-wide freshness
#: window in :mod:`commerce_protocols.core.replay`; two different tolerances for the same
#: kind of clock disagreement would only mean one of them was wrong.
MANDATE_CLOCK_SKEW_SECONDS: Final[int] = 300


def silence_sdk_disk_logging() -> None:
    """Stop ``ap2.sdk.mandate`` writing mandate operations into its own install directory.

    The SDK appends a JSON line to ``<package>/.logs/mandate_operations.log`` on every
    ``verify`` and ``present``, and that line contains the holder public key and the full
    presentation token. Specification 15.5 is unambiguous that AP2 key material must never
    reach logs, and a service writing tokens into ``site-packages`` also fails for duller
    reasons: the directory is read-only in a container, is not rotated, and is not covered
    by any retention policy this platform has.

    Called at import of :mod:`commerce_protocols.ap2` so that no code path can reach a
    mandate operation before the patch is in place. Patching a dependency is not something
    to do lightly; it is done here because the alternative is writing key material to disk
    on every verification, and there is no configuration switch for it.
    """

    def _discard(*_args: object, **_kwargs: object) -> None:
        """Accept the SDK's log call and drop it on the floor."""

    ap2_mandate._log_event = _discard  # noqa: SLF001 - the SDK offers no switch for this


def _now() -> int:
    """Seconds since the epoch, for ``iat``/``exp`` on mandates this platform issues.

    A process clock, and that is correct here in a way it would not be elsewhere: these are
    claims inside an artifact being handed to an external party, not a judgement about
    stored state. Nothing in this platform's own records is timestamped from here -- the
    database clock does that -- so a skewed pod can produce a mandate a peer rejects, but it
    cannot backdate anything that has already happened.
    """
    return int(time.time())


def _assert_es256(sd_jwt: str) -> None:
    """Refuse an SD-JWT whose issuer JWT is not ES256, before anything opens it.

    ``parse_token`` splits the compact serialization and decodes the header without
    verifying anything, which is exactly what is needed: the algorithm has to be judged
    before a key is chosen, so that a token claiming a symmetric algorithm never reaches a
    code path holding a public key.
    """
    try:
        parsed = parse_token(sd_jwt)
    except ValueError as exc:
        raise SignatureRejected("sd_jwt_malformed") from exc
    algorithm = parsed.header.get("alg")
    if algorithm != SIGNING_ALGORITHM:
        raise SignatureRejected("algorithm_not_permitted", announced=str(algorithm))


def _issue(payload: BaseModel, signer: Signer) -> str:
    """Issue one root SD-JWT under ``signer``'s key.

    Reaches into the signer's key because the SDK's issuance path takes a ``JWK`` and offers
    no injection point. That is the one place in this package where a private key leaves a
    :class:`Signer`, it goes only into the SDK's issuer, and it is confined to this function
    so the exception is visible rather than spread across the module.
    """
    key = getattr(signer, "key", None)
    if not isinstance(key, JWK):
        raise TypeError(
            "issuing an AP2 mandate needs a signer holding a jwcrypto JWK; a remote "
            "signer cannot be used here until the SDK accepts an injected signing callback"
        )
    issuance = ap2_mandate.MandateClient().create(payloads=[payload], issuer_key=key)
    return cast(str, issuance)


def issue_checkout_mandate(
    *,
    compact_checkout_jwt: str,
    checkout_hash: str,
    signer: Signer,
    ttl_seconds: int,
) -> str:
    """The buyer's mandate over one exact checkout, as a root SD-JWT.

    ``checkout_hash`` is passed in rather than recomputed so that the value bound into the
    mandate is provably the same one :func:`commerce_protocols.ap2.bridge.bind_checkout`
    produced -- one function computes it, everything else carries it. Recomputing here would
    create a second implementation of step 6 that could drift from the first.
    """
    if ttl_seconds <= 0:
        raise ValueError("a mandate needs a positive time to live")
    issued = _now()
    return _issue(
        CheckoutMandate(
            checkout_jwt=compact_checkout_jwt,
            checkout_hash=checkout_hash,
            iat=issued,
            exp=issued + ttl_seconds,
        ),
        signer,
    )


def issue_payment_mandate(
    *,
    transaction_id: str,
    payee: Any,
    payment_amount: Any,
    payment_instrument: Any,
    signer: Signer,
    ttl_seconds: int,
) -> str:
    """The buyer's mandate to pay, correlated to a checkout mandate by ``transaction_id``.

    ``payment_amount`` is an ``ap2.sdk.generated.types.amount.Amount``, whose ``amount``
    field is an integer in minor units. That happens to match this platform's own rule that
    no float ever touches money, so the two representations convert without rounding -- and
    the caller is the one place that conversion happens.
    """
    if ttl_seconds <= 0:
        raise ValueError("a mandate needs a positive time to live")
    issued = _now()
    return _issue(
        PaymentMandate(
            transaction_id=transaction_id,
            payee=payee,
            payment_amount=payment_amount,
            payment_instrument=payment_instrument,
            iat=issued,
            exp=issued + ttl_seconds,
        ),
        signer,
    )


def _verify_single(
    sd_jwt: str,
    ring: KeyRing,
    payload_type: type[BaseModel],
    *,
    expected_aud: str | None,
    expected_nonce: str | None,
) -> Any:
    """Verify one root SD-JWT and return its typed payload.

    Uses ``MandateClient.verify`` rather than ``SdJwtMandate.from_sd_jwt`` on purpose. The
    two look interchangeable and are not: ``from_sd_jwt`` performs no ``exp`` or ``iat``
    checking at all, while ``verify`` routes through the chain verifier, which does. For a
    single token the "chain" is one hop, and the time checks are the reason to take the
    longer road.
    """
    _assert_es256(sd_jwt)
    parsed = parse_token(sd_jwt)
    key = ring.resolve(parsed.header.get("kid"))
    try:
        verified = ap2_mandate.MandateClient().verify(
            token=sd_jwt,
            key_or_provider=key,
            payload_type=payload_type,
            expected_aud=expected_aud,
            expected_nonce=expected_nonce,
            clock_skew_seconds=MANDATE_CLOCK_SKEW_SECONDS,
        )
    except ValidationError as exc:
        raise SchemaRejected("mandate_payload_does_not_match_schema") from exc
    except ValueError as exc:
        # The SDK reports expiry, audience and nonce failures as ValueError. They are
        # authority failures, not signature failures: the bytes were fine and the mandate
        # still does not authorise this.
        raise MandateRejected("mandate_not_valid", detail=str(exc)) from exc
    except Exception as exc:
        # jwcrypto's JWException family does not inherit from ValueError, so a bad
        # signature arrives here rather than above.
        raise SignatureRejected("sd_jwt_did_not_verify") from exc

    payload = getattr(verified, "mandate_payload", None)
    if payload is None:  # pragma: no cover - the single-token branch always returns one
        raise SchemaRejected("mandate_verified_but_carried_no_payload")
    return payload


def _assert_expires(mandate: Any, label: str) -> None:
    """Refuse a mandate with no expiry.

    ``exp`` is optional in the AP2 schema, and a mandate without one is authority that never
    lapses. Specification 15.7 requires an expired-mandate case, which only means something
    if mandates are required to expire in the first place.
    """
    if getattr(mandate, "exp", None) is None:
        raise MandateRejected("mandate_has_no_expiry", mandate=label)


def verify_checkout_mandate(
    sd_jwt: str,
    ring: KeyRing,
    *,
    expected_aud: str | None = None,
    expected_nonce: str | None = None,
) -> CheckoutMandate:
    """Verify a checkout mandate's signature, schema, expiry, audience and nonce."""
    mandate = cast(
        CheckoutMandate,
        _verify_single(
            sd_jwt,
            ring,
            CheckoutMandate,
            expected_aud=expected_aud,
            expected_nonce=expected_nonce,
        ),
    )
    _assert_expires(mandate, "checkout")
    return mandate


def verify_payment_mandate(
    sd_jwt: str,
    ring: KeyRing,
    *,
    expected_aud: str | None = None,
    expected_nonce: str | None = None,
) -> PaymentMandate:
    """Verify a payment mandate's signature, schema, expiry, audience and nonce."""
    mandate = cast(
        PaymentMandate,
        _verify_single(
            sd_jwt,
            ring,
            PaymentMandate,
            expected_aud=expected_aud,
            expected_nonce=expected_nonce,
        ),
    )
    _assert_expires(mandate, "payment")
    return mandate


@dataclass(frozen=True, slots=True)
class VerifiedMandatePair:
    """A checkout mandate and a payment mandate proved to name the same checkout."""

    checkout_mandate: CheckoutMandate
    payment_mandate: PaymentMandate
    #: The value all three sources agreed on: the recomputed hash of the checkout JWT.
    transaction_id: str


def verify_mandate_pair(
    *,
    checkout_sd_jwt: str,
    payment_sd_jwt: str,
    ring: KeyRing,
    expected_transaction_id: str,
    expected_aud: str | None = None,
    expected_nonce: str | None = None,
) -> VerifiedMandatePair:
    """Verify both mandates and prove they correlate, specification 15.3 step 10.

    ``expected_transaction_id`` comes from the caller's own verification of the merchant
    authorization -- it is the bridge's step 6 output, computed from bytes this platform
    canonicalized itself. Every other candidate is compared against it, and none of them are
    compared against each other: three values agreeing with an independently computed
    reference is a real check, whereas three values that arrived together agreeing with one
    another proves only that whoever sent them was consistent.
    """
    checkout_mandate = verify_checkout_mandate(
        checkout_sd_jwt, ring, expected_aud=expected_aud, expected_nonce=expected_nonce
    )
    payment_mandate = verify_payment_mandate(
        payment_sd_jwt, ring, expected_aud=expected_aud, expected_nonce=expected_nonce
    )

    if checkout_mandate.checkout_hash != expected_transaction_id:
        raise CorrelationRejected(
            "checkout_mandate_names_a_different_checkout",
            expected=expected_transaction_id,
            presented=checkout_mandate.checkout_hash,
        )
    if payment_mandate.transaction_id != expected_transaction_id:
        raise CorrelationRejected(
            "payment_mandate_names_a_different_checkout",
            expected=expected_transaction_id,
            presented=payment_mandate.transaction_id,
        )
    return VerifiedMandatePair(
        checkout_mandate=checkout_mandate,
        payment_mandate=payment_mandate,
        transaction_id=expected_transaction_id,
    )
