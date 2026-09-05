"""The AP2 verification sequence end to end, specification 15.3 and 15.7.

`test_cp_ap2` covers the pieces -- signatures, mandates, receipts, the bridge. This file
covers the sequence that puts them together, and specifically the checks that only exist
once everything else has passed.

That distinction is the point. Steps 1 to 10 establish what the external party is claiming,
and every one of them can succeed against a perfectly-formed, correctly-signed, internally
consistent set of artifacts that authorises somebody else's purchase, or last week's price,
or a version this platform has already superseded. Step 11 is where the claim meets reality,
and the cases below are the ones where reality disagrees.

The ordering is itself a security property and is asserted here: state is read *after* the
cryptography, never before. A verifier that read state first would be reading state
identified by the caller's unverified fields, which is how one ends up confirming that an
attacker's mandate matches the attacker's chosen checkout.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from commerce_domain import Money, b64url, uuid7
from commerce_protocols.ap2 import mandates
from commerce_protocols.ap2.bridge import bind_checkout, compact_from_detached, jcs_payload
from commerce_protocols.ap2.signing import InProcessSigner, KeyRing
from commerce_protocols.ap2.verify import (
    PlatformCheckoutFacts,
    PresentedArtifacts,
    mandate_reference,
    verify_human_present,
)
from commerce_protocols.core.errors import (
    CorrelationRejected,
    MandateRejected,
    SchemaRejected,
    SignatureRejected,
    StateRejected,
)
from jwcrypto.jwk import JWK

CHECKOUT_ID = "chk_verify_1"
AUDIENCE = "https://merchant.demo.invalid"
TOTAL = 45990


def _checkout(total_minor: int = TOTAL, checkout_id: str = CHECKOUT_ID) -> dict[str, Any]:
    return {
        "id": checkout_id,
        "currency": "INR",
        "status": "ready_for_complete",
        "merchant": {"id": "mrc_demo", "name": "Demo Kirana"},
        "line_items": [
            {
                "id": "li_1",
                "item": {"id": "TEA-BEV-001", "title": "Masala chai", "price": 19900},
                "quantity": 2,
                "totals": [{"type": "subtotal", "amount": 39800}],
            }
        ],
        "totals": [{"type": "subtotal", "amount": 46300}, {"type": "total", "amount": total_minor}],
        "links": [],
    }


@pytest.fixture
def merchant(cp_merchant_key: JWK) -> InProcessSigner:
    return InProcessSigner.from_jwk(cp_merchant_key)


@pytest.fixture
def buyer(cp_buyer_key: JWK) -> InProcessSigner:
    return InProcessSigner.from_jwk(cp_buyer_key)


@pytest.fixture
def ring(merchant: InProcessSigner, buyer: InProcessSigner) -> KeyRing:
    return KeyRing.of(merchant, buyer)


def _artifacts(
    checkout: dict[str, Any],
    merchant: InProcessSigner,
    buyer: InProcessSigner,
    *,
    negotiated: bool = True,
    amount_minor: int | None = None,
    ttl: int = 600,
) -> PresentedArtifacts:
    """A complete, correctly-signed presentation over ``checkout``.

    ``amount_minor`` overrides only the *payment mandate's* amount, which is how the
    wrong-amount case is built: everything else stays consistent, so what fails is the one
    comparison being tested rather than an earlier signature check.
    """
    bound = bind_checkout(checkout, merchant)
    total = next(t["amount"] for t in checkout["totals"] if t["type"] == "total")
    return PresentedArtifacts(
        negotiated=negotiated,
        checkout=checkout,
        detached_merchant_authorization=bound.detached_merchant_authorization,
        checkout_mandate_sd_jwt=mandates.issue_checkout_mandate(
            compact_checkout_jwt=bound.compact_checkout_jwt,
            checkout_hash=bound.transaction_id,
            signer=buyer,
            ttl_seconds=ttl,
        ),
        payment_mandate_sd_jwt=mandates.issue_payment_mandate(
            transaction_id=bound.transaction_id,
            payee=Merchant(id="mrc_demo", name="Demo Kirana"),
            payment_amount=Amount(
                amount=total if amount_minor is None else amount_minor, currency="INR"
            ),
            payment_instrument=PaymentInstrument(id="pi_upi", type="upi"),
            signer=buyer,
            ttl_seconds=ttl,
        ),
        audience=AUDIENCE,
    )


def _facts(
    *,
    total_minor: int = TOTAL,
    version: int = 3,
    latest_version: int | None = None,
    external_id: str = CHECKOUT_ID,
) -> PlatformCheckoutFacts:
    return PlatformCheckoutFacts(
        tenant_id=uuid.uuid4(),
        merchant_id=uuid7(),
        checkout_id=uuid7(),
        version=version,
        content_hash="stored-content-hash",
        amount=Money(total_minor, "INR"),
        authority_epoch=7,
        latest_version=version if latest_version is None else latest_version,
        merchant_external_id=external_id,
    )


def _verify(artifacts: PresentedArtifacts, facts: PlatformCheckoutFacts, ring: KeyRing) -> Any:
    return verify_human_present(
        artifacts,
        facts,
        ring,
        correlation_id=uuid7(),
        verification_receipt_id="vr_1",
        issuer="platform.demo.invalid",
    )


class TestTheHappyPathProducesAProof:
    def test_a_complete_presentation_yields_a_verified_authority_proof(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Step 12. The only thing in this package that can produce a proof."""
        checkout = _checkout()
        facts = _facts()
        outcome = _verify(_artifacts(checkout, merchant, buyer), facts, ring)

        proof = outcome.proof
        assert proof.protocol == "AP2"
        assert proof.protocol_version == "v0.2.0"
        assert proof.algorithm == "ES256"
        assert proof.key_id == buyer.kid
        assert proof.tenant_id == facts.tenant_id
        assert proof.merchant_id == facts.merchant_id
        assert proof.checkout.version == facts.version
        assert proof.checkout.content_hash == facts.content_hash
        assert proof.amount == Money(TOTAL, "INR")
        assert proof.authority_epoch == 7
        assert proof.action == "PAYMENT_CREATE_ORDER"

    def test_the_proof_carries_the_kernels_own_money_type(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Integer minor units all the way from the mandate into the kernel's contract."""
        outcome = _verify(_artifacts(_checkout(), merchant, buyer), _facts(), ring)
        assert isinstance(outcome.proof.amount.minor, int)
        assert outcome.proof.amount.minor == TOTAL

    def test_the_receipt_reference_is_derived_from_the_checkout_jwt(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """One value every participant already agrees on, so a receipt ties back to it.

        The expected value is reconstructed from the *same* presentation rather than by
        signing the checkout a second time, and that is not incidental: ECDSA is
        randomised, so two signatures over identical bytes differ, and a second
        ``bind_checkout`` would produce a different compact JWT and therefore a different
        reference. This is the same property that makes the golden vector verify a stored
        signature rather than re-derive one.
        """
        checkout = _checkout()
        artifacts = _artifacts(checkout, merchant, buyer)
        outcome = _verify(artifacts, _facts(), ring)

        payload_b64 = b64url(jcs_payload(checkout))
        compact = compact_from_detached(artifacts.detached_merchant_authorization, payload_b64)
        assert outcome.reference == mandate_reference(compact)


class TestStateIsComparedAfterTheCryptography:
    def test_a_mandate_naming_an_amount_the_checkout_no_longer_costs_is_refused(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Specification 15.7's wrong-amount case.

        Everything cryptographic passes. The merchant signed this checkout, the buyer signed
        both mandates, and they correlate. The price has moved since, and that is the only
        thing wrong -- which is exactly the situation the kernel's REAPPROVAL_REQUIRED path
        exists for.
        """
        checkout = _checkout(total_minor=45990)
        facts = _facts(total_minor=52990)

        with pytest.raises(StateRejected) as caught:
            _verify(_artifacts(checkout, merchant, buyer), facts, ring)
        assert caught.value.reason == "mandate_amount_does_not_match_current_total"
        assert caught.value.details["presented_minor"] == 45990
        assert caught.value.details["current_minor"] == 52990

    def test_a_payment_mandate_for_a_different_amount_than_the_checkout_is_refused(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """The payment mandate is checked against platform state, not against the checkout.

        A mandate authorising less than the checkout costs would otherwise pass, because it
        correlates correctly and the checkout itself is priced correctly.
        """
        checkout = _checkout()
        with pytest.raises(CorrelationRejected) as caught:
            _verify(_artifacts(checkout, merchant, buyer, amount_minor=39900), _facts(), ring)
        assert caught.value.reason == "payment_mandate_amount_does_not_match"

    def test_a_version_n_mandate_is_refused_once_version_n_plus_one_exists(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Specification 15.7 names this case explicitly.

        The mandate was valid when it was made. A newer version exists, so the consent it
        carries is consent to a checkout that no longer governs the purchase.
        """
        facts = _facts(version=3, latest_version=4)
        with pytest.raises(StateRejected) as caught:
            _verify(_artifacts(_checkout(), merchant, buyer), facts, ring)
        assert caught.value.reason == "mandate_names_a_superseded_version"
        assert caught.value.details["latest_version"] == 4

    def test_a_mandate_for_another_merchants_checkout_is_refused(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        checkout = _checkout(checkout_id="chk_somebody_else")
        with pytest.raises(StateRejected) as caught:
            _verify(_artifacts(checkout, merchant, buyer), _facts(), ring)
        assert caught.value.reason == "checkout_identifier_does_not_match_platform_state"


class TestNegotiationAndSchema:
    def test_a_presentation_is_refused_when_ap2_was_never_negotiated(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Step 1, and it is a fact about our session record rather than about the request.

        A caller cannot negotiate on its own behalf by asserting that it did.
        """
        artifacts = _artifacts(_checkout(), merchant, buyer, negotiated=False)
        with pytest.raises(MandateRejected) as caught:
            _verify(artifacts, _facts(), ring)
        assert caught.value.reason == "ap2_was_not_negotiated_for_this_session"

    def test_negotiation_is_checked_before_any_signature_work(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Cheap refusals first. A garbage presentation that was never negotiated is
        refused on the negotiation flag rather than spending an elliptic-curve verification
        on it."""
        artifacts = PresentedArtifacts(
            negotiated=False,
            checkout=_checkout(),
            detached_merchant_authorization="not-a-jws",
            checkout_mandate_sd_jwt="not-an-sd-jwt",
            payment_mandate_sd_jwt="not-an-sd-jwt",
            audience=AUDIENCE,
        )
        with pytest.raises(MandateRejected) as caught:
            _verify(artifacts, _facts(), ring)
        assert caught.value.reason == "ap2_was_not_negotiated_for_this_session"

    @pytest.mark.parametrize("bad_total", [459.9, "45990", None, True])
    def test_a_checkout_whose_total_is_not_an_integer_is_refused(
        self,
        merchant: InProcessSigner,
        buyer: InProcessSigner,
        ring: KeyRing,
        bad_total: Any,
    ) -> None:
        """No float ever touches an amount, and ``True`` is excluded by name."""
        checkout = _checkout()
        checkout["totals"] = [{"type": "total", "amount": bad_total}]
        artifacts = _artifacts(_checkout(), merchant, buyer)
        broken = PresentedArtifacts(
            negotiated=True,
            checkout=checkout,
            detached_merchant_authorization=artifacts.detached_merchant_authorization,
            checkout_mandate_sd_jwt=artifacts.checkout_mandate_sd_jwt,
            payment_mandate_sd_jwt=artifacts.payment_mandate_sd_jwt,
            audience=AUDIENCE,
        )
        with pytest.raises(SchemaRejected) as caught:
            _verify(broken, _facts(), ring)
        assert caught.value.reason == "checkout_total_is_not_an_integer_minor_amount"

    def test_a_checkout_with_no_total_component_is_refused(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        checkout = _checkout()
        checkout["totals"] = [{"type": "subtotal", "amount": 46300}]
        artifacts = _artifacts(_checkout(), merchant, buyer)
        broken = PresentedArtifacts(
            negotiated=True,
            checkout=checkout,
            detached_merchant_authorization=artifacts.detached_merchant_authorization,
            checkout_mandate_sd_jwt=artifacts.checkout_mandate_sd_jwt,
            payment_mandate_sd_jwt=artifacts.payment_mandate_sd_jwt,
            audience=AUDIENCE,
        )
        with pytest.raises(SchemaRejected) as caught:
            _verify(broken, _facts(), ring)
        assert caught.value.reason == "checkout_has_no_total_component"


class TestNoProofSurvivesAFailedCheck:
    def test_a_tampered_checkout_produces_no_proof(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """The merchant authorization is verified against our own canonicalization."""
        artifacts = _artifacts(_checkout(), merchant, buyer)
        tampered = dict(artifacts.checkout)
        tampered["totals"] = [
            {"type": "subtotal", "amount": 46300},
            {"type": "total", "amount": TOTAL + 1},
        ]
        broken = PresentedArtifacts(
            negotiated=True,
            checkout=tampered,
            detached_merchant_authorization=artifacts.detached_merchant_authorization,
            checkout_mandate_sd_jwt=artifacts.checkout_mandate_sd_jwt,
            payment_mandate_sd_jwt=artifacts.payment_mandate_sd_jwt,
            audience=AUDIENCE,
        )
        with pytest.raises(SignatureRejected):
            _verify(broken, _facts(total_minor=TOTAL + 1), ring)

    def test_a_mandate_signed_by_an_untrusted_buyer_produces_no_proof(
        self, merchant: InProcessSigner, cp_foreign_key: JWK, ring: KeyRing
    ) -> None:
        stranger = InProcessSigner.from_jwk(cp_foreign_key)
        artifacts = _artifacts(_checkout(), merchant, stranger)
        with pytest.raises(SignatureRejected) as caught:
            _verify(artifacts, _facts(), ring)
        assert caught.value.reason == "unknown_kid"

    def test_an_expired_mandate_produces_no_proof(
        self, merchant: InProcessSigner, buyer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Expiry is enforced, and 15.3's rule is that nothing partial escapes."""
        import time

        from ap2.sdk.generated.checkout_mandate import CheckoutMandate

        checkout = _checkout()
        bound = bind_checkout(checkout, merchant)
        issued = int(time.time()) - 4000
        artifacts = _artifacts(checkout, merchant, buyer)
        expired = PresentedArtifacts(
            negotiated=True,
            checkout=checkout,
            detached_merchant_authorization=artifacts.detached_merchant_authorization,
            checkout_mandate_sd_jwt=mandates._issue(  # noqa: SLF001 - an expired artifact
                CheckoutMandate(
                    checkout_jwt=bound.compact_checkout_jwt,
                    checkout_hash=bound.transaction_id,
                    iat=issued,
                    exp=issued + 60,
                ),
                buyer,
            ),
            payment_mandate_sd_jwt=artifacts.payment_mandate_sd_jwt,
            audience=AUDIENCE,
        )
        with pytest.raises(MandateRejected):
            _verify(expired, _facts(), ring)

    def test_verify_human_present_is_the_only_producer_of_a_proof(self) -> None:
        """Structural, per 15.3: no partial path yields authority.

        Asserted over the package rather than by exercising each function, because the
        property is that no *other* function returns one -- an absence a behavioural test
        cannot establish.
        """
        import inspect

        import commerce_protocols.ap2 as ap2_package
        from transaction_kernel import VerifiedAuthorityProof

        producers = []
        for module_name in ("bridge", "mandates", "receipts", "signing", "vectors", "verify"):
            module = __import__(
                f"commerce_protocols.ap2.{module_name}", fromlist=[module_name]
            ).__dict__
            for name, value in module.items():
                if name.startswith("_") or not inspect.isfunction(value):
                    continue
                annotation = inspect.signature(value).return_annotation
                if VerifiedAuthorityProof.__name__ in str(annotation):
                    producers.append(f"{module_name}.{name}")

        assert producers == [], (
            f"{producers} return a VerifiedAuthorityProof directly; only "
            "verify_human_present may produce one, and it returns a VerificationOutcome "
            "so the proof cannot be built without the artifacts it rests on"
        )
        assert ap2_package is not None
