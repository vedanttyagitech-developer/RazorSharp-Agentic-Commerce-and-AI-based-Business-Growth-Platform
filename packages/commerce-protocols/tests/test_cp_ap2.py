"""The AP2 conformance suite, specification 15.7.

Specification 15.7 ends with a sentence this file exists to earn: "Only claim 'AP2
implemented' after these tests and the end-to-end path pass." So the cases below are the
ones it lists, and where a case cannot be covered at this layer the test says so by name
rather than being quietly omitted.

Three of the assertions here are worth reading even if the rest are skimmed.

``TestAlgorithmPinning`` demonstrates a live forgery. It signs a token with ``HS256`` using
the *published* public key as the HMAC secret, shows that the pinned AP2 SDK's own
``verify_jwt`` accepts it, and shows that this package refuses it. Publishing verification
keys is mandatory (specification 14.1), so the secret in that attack is public by design;
the only thing standing between that fact and a forged merchant authorization is the
algorithm pin.

``TestGoldenVector`` is the whole of specification 15.4's status claim. Until it passes, the
byte-level bridge is an implementation target and the status table must say so.

``TestNoPrematureReceipt`` is specification 15.7's "no premature success receipt", and it
asserts the property structurally: the payment-receipt function cannot be reached without a
capture proof, and capture proofs cannot be built for a payment that has not captured.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.jwt_helper import verify_jwt as sdk_verify_jwt
from ap2.sdk.utils import compute_sha256_b64url
from commerce_domain import Money, b64url
from commerce_protocols.ap2 import mandates
from commerce_protocols.ap2.bridge import (
    bind_checkout,
    compact_from_detached,
    detach,
    jcs_payload,
    transaction_id_for,
    verify_merchant_authorization,
)
from commerce_protocols.ap2.receipts import (
    capture_proof,
    issue_checkout_receipt,
    issue_payment_receipt,
    issue_rejection_receipt,
    verify_receipt,
)
from commerce_protocols.ap2.signing import InProcessSigner, KeyRing, verify_compact
from commerce_protocols.ap2.vectors import (
    GOLDEN_VECTOR_PATH,
    REFERENCE_CHECKOUT,
    VectorMismatch,
    check_vector,
    load_vector,
)
from commerce_protocols.core.errors import (
    CorrelationRejected,
    MandateRejected,
    SchemaRejected,
    SignatureRejected,
)
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS
from transaction_kernel import PaymentState

# ---- helpers


def _checkout(total_minor: int = 39800, checkout_id: str = "chk_1") -> dict[str, Any]:
    """A minimal, schema-valid UCP checkout carrying an integer total."""
    return {
        "id": checkout_id,
        "currency": "INR",
        "status": "ready_for_complete",
        "line_items": [
            {
                "id": "li_1",
                "item": {"id": "TEA-BEV-001", "title": "Masala chai", "price": 19900},
                "quantity": 2,
                "totals": [{"type": "subtotal", "amount": total_minor}],
            }
        ],
        "totals": [{"type": "total", "amount": total_minor}],
        "links": [],
    }


@pytest.fixture
def merchant_signer(cp_merchant_key: JWK) -> InProcessSigner:
    return InProcessSigner.from_jwk(cp_merchant_key)


@pytest.fixture
def buyer_signer(cp_buyer_key: JWK) -> InProcessSigner:
    return InProcessSigner.from_jwk(cp_buyer_key)


@pytest.fixture
def platform_signer(cp_platform_key: JWK) -> InProcessSigner:
    return InProcessSigner.from_jwk(cp_platform_key)


@pytest.fixture
def ring(
    merchant_signer: InProcessSigner,
    buyer_signer: InProcessSigner,
    platform_signer: InProcessSigner,
) -> KeyRing:
    return KeyRing.of(merchant_signer, buyer_signer, platform_signer)


def _mandate_pair(
    checkout: dict[str, Any], merchant: InProcessSigner, buyer: InProcessSigner, ttl: int = 600
) -> tuple[str, str, str]:
    """A correlated checkout mandate and payment mandate over ``checkout``."""
    bound = bind_checkout(checkout, merchant)
    total = next(t["amount"] for t in checkout["totals"] if t["type"] == "total")
    checkout_mandate = mandates.issue_checkout_mandate(
        compact_checkout_jwt=bound.compact_checkout_jwt,
        checkout_hash=bound.transaction_id,
        signer=buyer,
        ttl_seconds=ttl,
    )
    payment_mandate = mandates.issue_payment_mandate(
        transaction_id=bound.transaction_id,
        payee=Merchant(id="mrc_demo", name="Demo"),
        payment_amount=Amount(amount=total, currency=checkout["currency"]),
        payment_instrument=PaymentInstrument(id="pi_upi", type="upi"),
        signer=buyer,
        ttl_seconds=ttl,
    )
    return checkout_mandate, payment_mandate, bound.transaction_id


# ---- the detached merchant authorization, and tamper cases


class TestDetachedMerchantAuthorization:
    def test_a_valid_detached_authorization_verifies_and_carries_no_payload(
        self, merchant_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Specification 15.2: the merchant JWS is detached, in ``header..signature`` form."""
        checkout = _checkout()
        bound = bind_checkout(checkout, merchant_signer)

        header, payload, signature = bound.detached_merchant_authorization.split(".")
        assert payload == "", "a detached JWS carries no payload segment"
        assert header and signature

        again = verify_merchant_authorization(checkout, bound.detached_merchant_authorization, ring)
        assert again.transaction_id == bound.transaction_id

    def test_the_signature_covers_the_checkout_without_its_ap2_member(
        self, merchant_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Step 1: a signature cannot cover the container it will be stored in.

        The authorization must verify against the *assembled* checkout -- the one that now
        carries the signature inside ``ap2`` -- because that is the document a peer holds.
        """
        checkout = _checkout()
        bound = bind_checkout(checkout, merchant_signer)

        assembled = dict(checkout)
        assembled["ap2"] = {"merchant_authorization": bound.detached_merchant_authorization}

        verified = verify_merchant_authorization(
            assembled, bound.detached_merchant_authorization, ring
        )
        assert verified.transaction_id == bound.transaction_id
        assert b'"ap2"' not in verified.jcs_bytes

    def test_a_one_paisa_change_to_the_total_breaks_the_signature(
        self, merchant_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Specification 29.1's rounding rule, at the protocol edge.

        One paisa is the smallest change that can be made, and it must be caught. A bridge
        that normalised or rounded anywhere would pass a larger tamper and fail this.
        """
        checkout = _checkout(total_minor=39800)
        bound = bind_checkout(checkout, merchant_signer)

        tampered = json.loads(json.dumps(checkout))
        tampered["totals"][0]["amount"] = 39801

        with pytest.raises(SignatureRejected) as caught:
            verify_merchant_authorization(tampered, bound.detached_merchant_authorization, ring)
        assert caught.value.reason == "signature_did_not_verify"

    def test_reordering_members_does_not_break_the_signature(
        self, merchant_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """JCS is the reason. The same semantic document must produce the same bytes.

        Without canonicalization this would fail, and it would fail *intermittently* --
        whenever a JSON library, a proxy or a language's dict ordering happened to differ.
        """
        checkout = _checkout()
        bound = bind_checkout(checkout, merchant_signer)

        reordered = dict(reversed(list(checkout.items())))
        assert list(reordered) != list(checkout)

        verified = verify_merchant_authorization(
            reordered, bound.detached_merchant_authorization, ring
        )
        assert verified.transaction_id == bound.transaction_id

    def test_a_signature_from_an_untrusted_key_is_refused(
        self, cp_foreign_key: JWK, ring: KeyRing
    ) -> None:
        """A perfectly valid signature from a key nobody trusts is still not authority."""
        foreign = InProcessSigner.from_jwk(cp_foreign_key)
        checkout = _checkout()
        bound = bind_checkout(checkout, foreign)

        with pytest.raises(SignatureRejected) as caught:
            verify_merchant_authorization(checkout, bound.detached_merchant_authorization, ring)
        assert caught.value.reason == "unknown_kid"

    def test_a_detached_form_with_a_payload_is_refused(
        self, merchant_signer: InProcessSigner
    ) -> None:
        """``header.payload.signature`` is not a detached JWS and must not be treated as one."""
        bound = bind_checkout(_checkout(), merchant_signer)
        with pytest.raises(SignatureRejected) as caught:
            compact_from_detached(bound.compact_checkout_jwt, bound.payload_b64)
        assert caught.value.reason == "not_a_detached_jws"

    def test_detach_refuses_something_that_is_not_a_compact_jws(self) -> None:
        with pytest.raises(SignatureRejected) as caught:
            detach("not.a.jws.at.all")
        assert caught.value.reason == "not_a_compact_jws"


# ---- algorithm pinning: the attack the SDK does not defend against


class TestAlgorithmPinning:
    """Specification 15.1: "Reject unexpected algorithms."

    The pinned SDK does not, and this class demonstrates both halves of that fact.
    """

    @staticmethod
    def _hs256_forgery(public_jwk: dict[str, Any], kid: str) -> tuple[str, JWK]:
        """A token signed with HS256, keyed on the published public key.

        This is the classic JWS algorithm-confusion forgery. The attacker needs no secret:
        specification 14.1 requires the verification keys to be published, so the bytes
        used as the HMAC key here are ones anybody can fetch.
        """
        secret = JWK(
            kty="oct",
            k=b64url(json.dumps(public_jwk, sort_keys=True, separators=(",", ":")).encode()),
        )
        forged = JWS(json.dumps({"id": "chk_evil", "totals": []}).encode())
        forged.add_signature(
            secret, alg="HS256", protected=json.dumps({"alg": "HS256", "kid": kid})
        )
        return str(forged.serialize(compact=True)), secret

    def test_the_pinned_sdk_accepts_an_hs256_forgery(
        self, merchant_signer: InProcessSigner
    ) -> None:
        """Characterising the dependency, so the pin below is visibly load-bearing.

        If a future AP2 release fixes this, the test fails and tells us the SDK moved --
        which is exactly what a pin is supposed to make visible.
        """
        token, secret = self._hs256_forgery(merchant_signer.public_jwk(), merchant_signer.kid)
        accepted = sdk_verify_jwt(token, secret)
        assert accepted["id"] == "chk_evil", (
            "ap2.sdk.jwt_helper.verify_jwt passes no alg to jwcrypto, so it honours the "
            "algorithm named in the attacker's own header"
        )

    def test_our_verifier_refuses_the_same_forgery_before_touching_a_key(
        self, merchant_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        token, _ = self._hs256_forgery(merchant_signer.public_jwk(), merchant_signer.kid)
        with pytest.raises(SignatureRejected) as caught:
            verify_compact(token, ring)
        assert caught.value.reason == "algorithm_not_permitted"
        assert caught.value.details["announced"] == "HS256"

    def test_alg_none_is_refused(self, merchant_signer: InProcessSigner, ring: KeyRing) -> None:
        header = b64url(json.dumps({"alg": "none", "kid": merchant_signer.kid}).encode())
        token = f"{header}.{b64url(b'{}')}."
        with pytest.raises(SignatureRejected) as caught:
            verify_compact(token, ring)
        assert caught.value.reason == "algorithm_not_permitted"

    def test_a_missing_kid_is_refused_even_when_the_ring_holds_one_key(
        self, merchant_signer: InProcessSigner
    ) -> None:
        """Guessing works until a second key exists, and then it silently changes meaning."""
        single = KeyRing.of(merchant_signer)
        bound = bind_checkout(_checkout(), merchant_signer)
        header, payload, signature = bound.compact_checkout_jwt.split(".")
        stripped = b64url(json.dumps({"alg": "ES256"}).encode())
        with pytest.raises(SignatureRejected) as caught:
            verify_compact(f"{stripped}.{payload}.{signature}", single)
        assert caught.value.reason == "protected_header_carries_no_kid"

    def test_a_rejection_never_reveals_which_stage_of_the_crypto_failed(
        self, merchant_signer: InProcessSigner, cp_foreign_key: JWK, ring: KeyRing
    ) -> None:
        """A verifier that distinguishes 'bad key' from 'bad bytes' is an oracle."""
        foreign = InProcessSigner.from_jwk(cp_foreign_key)
        good = bind_checkout(_checkout(), merchant_signer)
        header, payload, _ = good.compact_checkout_jwt.split(".")
        wrong_signature = bind_checkout(_checkout(), foreign).compact_checkout_jwt.split(".")[2]

        with pytest.raises(SignatureRejected) as caught:
            verify_compact(f"{header}.{payload}.{wrong_signature}", ring)
        assert caught.value.reason == "signature_did_not_verify"
        assert caught.value.details == {}, "a refusal must not describe the key material"


# ---- key rotation


class TestKeyRotation:
    def test_a_retired_key_still_verifies_evidence_it_signed(
        self, merchant_signer: InProcessSigner, cp_foreign_key: JWK
    ) -> None:
        """Specification 14.1: rotate keys without silently invalidating stored evidence.

        The old key leaves the *signer* and stays in the *ring*. A ring that only held the
        current key would fail every historical verification the moment a rotation
        happened, and would fail it as an ordinary bad-signature result -- so nobody would
        recognise it as a rotation problem.
        """
        historical = bind_checkout(_checkout(), merchant_signer)

        rotated = InProcessSigner.from_jwk(cp_foreign_key)
        ring_after_rotation = KeyRing.of(merchant_signer, rotated)

        verified = verify_merchant_authorization(
            _checkout(), historical.detached_merchant_authorization, ring_after_rotation
        )
        assert verified.transaction_id == historical.transaction_id

    def test_two_signers_sharing_a_kid_are_refused(self, cp_merchant_key: JWK) -> None:
        """A kid must resolve to one key, or attribution means nothing."""
        one = InProcessSigner.from_jwk(cp_merchant_key)
        two = InProcessSigner.from_jwk(cp_merchant_key)
        with pytest.raises(ValueError, match="share kid"):
            KeyRing.of(one, two)

    def test_a_public_key_cannot_become_a_signer(self, cp_merchant_key: JWK) -> None:
        public = JWK(**json.loads(cp_merchant_key.export_public()))
        with pytest.raises(ValueError, match="no private component"):
            InProcessSigner.from_jwk(public)

    def test_a_signing_key_without_a_kid_is_refused(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec

        anonymous = JWK.from_pyca(ec.generate_private_key(ec.SECP256R1()))
        with pytest.raises(ValueError, match="carries no kid"):
            InProcessSigner.from_jwk(anonymous)

    def test_the_published_jwk_set_never_contains_a_private_component(self, ring: KeyRing) -> None:
        """Specification 15.5. The one assertion whose failure is unrecoverable."""
        published = json.dumps(ring.public_jwks())
        assert '"d"' not in published
        for key in ring.public_jwks()["keys"]:
            assert "d" not in key


# ---- the golden vector, specification 15.4


class TestGoldenVector:
    def test_the_committed_vector_reproduces_byte_for_byte(self) -> None:
        """Specification 15.4's status gate. Until this passes the bridge is unverified."""
        report = check_vector(load_vector())
        assert report.signature_verified
        assert report.sdk_agrees_on_transaction_id
        assert set(report.fields_checked) == {
            "jcs_utf8",
            "jcs_byte_length",
            "payload_b64",
            "protected_header_json",
            "compact_checkout_jwt",
            "transaction_id",
        }

    def test_the_vector_contains_no_private_key(self) -> None:
        """ "Never include a private key in fixtures." Asserted over the raw file bytes."""
        raw = GOLDEN_VECTOR_PATH.read_text(encoding="utf-8")
        assert '"d"' not in raw
        for key in load_vector()["public_keys"]["keys"]:
            assert set(key) <= {"kty", "crv", "x", "y", "kid", "alg", "use"}
            assert "d" not in key

    def test_the_vector_exercises_non_ascii_and_keeps_it_as_utf8(self) -> None:
        """JCS does not \\u-escape. ``json.dumps`` defaults do, and that is the divergence."""
        vector = load_vector()
        assert "किराना" in vector["jcs_utf8"]
        assert "\\u" not in vector["jcs_utf8"]
        assert vector["jcs_byte_length"] == len(vector["jcs_utf8"].encode("utf-8"))

    def test_the_vector_strips_the_ap2_member(self) -> None:
        assert "ap2" in REFERENCE_CHECKOUT
        assert '"ap2"' not in load_vector()["jcs_utf8"]

    def test_a_changed_input_checkout_fails_the_vector_and_names_the_step(self) -> None:
        """The vector must actually be able to fail, and say where."""
        vector = dict(load_vector())
        broken = json.loads(json.dumps(vector["input_checkout"]))
        broken["totals"][-1]["amount"] += 1
        vector["input_checkout"] = broken

        with pytest.raises(VectorMismatch) as caught:
            check_vector(vector)
        assert caught.value.field == "jcs_utf8"

    def test_the_transaction_id_is_the_hash_of_the_compact_jwt(self) -> None:
        vector = load_vector()
        assert vector["transaction_id"] == transaction_id_for(vector["compact_checkout_jwt"])
        assert vector["transaction_id"] == compute_sha256_b64url(vector["compact_checkout_jwt"])

    def test_jcs_is_stable_across_repeated_serialization(self) -> None:
        first = jcs_payload(REFERENCE_CHECKOUT)
        second = jcs_payload(json.loads(json.dumps(REFERENCE_CHECKOUT)))
        assert first == second


# ---- mandates: expiry, audience, correlation


class TestMandates:
    def test_a_correlated_pair_verifies(
        self, merchant_signer: InProcessSigner, buyer_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        checkout = _checkout()
        checkout_mandate, payment_mandate, transaction_id = _mandate_pair(
            checkout, merchant_signer, buyer_signer
        )
        pair = mandates.verify_mandate_pair(
            checkout_sd_jwt=checkout_mandate,
            payment_sd_jwt=payment_mandate,
            ring=ring,
            expected_transaction_id=transaction_id,
        )
        assert pair.transaction_id == transaction_id
        assert pair.payment_mandate.payment_amount.amount == 39800
        assert isinstance(pair.payment_mandate.payment_amount.amount, int)

    def test_the_human_present_mandate_is_a_single_root_sd_jwt(
        self, merchant_signer: InProcessSigner, buyer_signer: InProcessSigner
    ) -> None:
        """Not a chain. ``~~`` would mean a delegated flow, which this is not."""
        checkout_mandate, payment_mandate, _ = _mandate_pair(
            _checkout(), merchant_signer, buyer_signer
        )
        assert "~~" not in checkout_mandate
        assert "~~" not in payment_mandate

    def test_a_mandate_for_a_different_checkout_is_refused(
        self, merchant_signer: InProcessSigner, buyer_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Specification 15.7's "wrong-checkout" case.

        The mandates are internally consistent and correctly signed. They are for another
        purchase, and that is the only thing wrong with them.
        """
        checkout_mandate, payment_mandate, _ = _mandate_pair(
            _checkout(total_minor=39800), merchant_signer, buyer_signer
        )
        other = bind_checkout(_checkout(total_minor=3980000, checkout_id="chk_2"), merchant_signer)

        with pytest.raises(CorrelationRejected) as caught:
            mandates.verify_mandate_pair(
                checkout_sd_jwt=checkout_mandate,
                payment_sd_jwt=payment_mandate,
                ring=ring,
                expected_transaction_id=other.transaction_id,
            )
        assert caught.value.reason == "checkout_mandate_names_a_different_checkout"

    def test_a_payment_mandate_naming_another_transaction_is_refused(
        self, merchant_signer: InProcessSigner, buyer_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """The correlation is checked on both mandates, not just the first one seen."""
        checkout = _checkout()
        bound = bind_checkout(checkout, merchant_signer)
        checkout_mandate = mandates.issue_checkout_mandate(
            compact_checkout_jwt=bound.compact_checkout_jwt,
            checkout_hash=bound.transaction_id,
            signer=buyer_signer,
            ttl_seconds=600,
        )
        mismatched_payment = mandates.issue_payment_mandate(
            transaction_id="not-the-right-hash",
            payee=Merchant(id="mrc_demo", name="Demo"),
            payment_amount=Amount(amount=39800, currency="INR"),
            payment_instrument=PaymentInstrument(id="pi_upi", type="upi"),
            signer=buyer_signer,
            ttl_seconds=600,
        )
        with pytest.raises(CorrelationRejected) as caught:
            mandates.verify_mandate_pair(
                checkout_sd_jwt=checkout_mandate,
                payment_sd_jwt=mismatched_payment,
                ring=ring,
                expected_transaction_id=bound.transaction_id,
            )
        assert caught.value.reason == "payment_mandate_names_a_different_checkout"

    def test_an_expired_mandate_is_refused(
        self, merchant_signer: InProcessSigner, buyer_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """Specification 15.7's expired case.

        The TTL is far enough past the SDK's 300-second clock-skew allowance that the
        refusal cannot be an artefact of the tolerance.
        """
        bound = bind_checkout(_checkout(), merchant_signer)
        issued = int(time.time()) - 4000
        expired = mandates._issue(  # noqa: SLF001 - constructing an expired artifact on purpose
            CheckoutMandate(
                checkout_jwt=bound.compact_checkout_jwt,
                checkout_hash=bound.transaction_id,
                iat=issued,
                exp=issued + 60,
            ),
            buyer_signer,
        )
        with pytest.raises(MandateRejected) as caught:
            mandates.verify_checkout_mandate(expired, ring)
        assert caught.value.reason == "mandate_not_valid"

    def test_a_mandate_with_no_expiry_is_refused(
        self, merchant_signer: InProcessSigner, buyer_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """``exp`` is optional in the AP2 schema. Authority that never lapses is not authority
        this platform will accept."""
        bound = bind_checkout(_checkout(), merchant_signer)
        eternal = mandates._issue(  # noqa: SLF001 - the SDK has no way to omit exp otherwise
            CheckoutMandate(
                checkout_jwt=bound.compact_checkout_jwt, checkout_hash=bound.transaction_id
            ),
            buyer_signer,
        )
        with pytest.raises(MandateRejected) as caught:
            mandates.verify_checkout_mandate(eternal, ring)
        assert caught.value.reason == "mandate_has_no_expiry"

    def test_a_mandate_signed_by_an_unknown_key_is_refused(
        self, merchant_signer: InProcessSigner, cp_foreign_key: JWK, ring: KeyRing
    ) -> None:
        stranger = InProcessSigner.from_jwk(cp_foreign_key)
        checkout_mandate, _, _ = _mandate_pair(_checkout(), merchant_signer, stranger)
        with pytest.raises(SignatureRejected) as caught:
            mandates.verify_checkout_mandate(checkout_mandate, ring)
        assert caught.value.reason == "unknown_kid"

    def test_a_malformed_sd_jwt_is_refused_as_a_signature_failure(self, ring: KeyRing) -> None:
        with pytest.raises(SignatureRejected):
            mandates.verify_checkout_mandate("this-is-not-an-sd-jwt", ring)

    def test_a_ttl_of_zero_is_refused_at_issuance(
        self, merchant_signer: InProcessSigner, buyer_signer: InProcessSigner
    ) -> None:
        bound = bind_checkout(_checkout(), merchant_signer)
        with pytest.raises(ValueError, match="positive time to live"):
            mandates.issue_checkout_mandate(
                compact_checkout_jwt=bound.compact_checkout_jwt,
                checkout_hash=bound.transaction_id,
                signer=buyer_signer,
                ttl_seconds=0,
            )


# ---- receipts, and the premature-receipt prohibition


class TestNoPrematureReceipt:
    """Specification 15.2 and 15.7: payment receipts only after verified payment capture."""

    @pytest.mark.parametrize(
        "state",
        [
            PaymentState.CREATED,
            PaymentState.SUBMITTED,
            PaymentState.AUTHORIZED,
            PaymentState.FAILED,
            PaymentState.UNKNOWN,
            PaymentState.RECONCILING,
            PaymentState.ESCALATED,
            PaymentState.EXPIRED,
        ],
    )
    def test_no_capture_proof_exists_for_any_uncaptured_state(self, state: PaymentState) -> None:
        """``AUTHORIZED`` is the one worth staring at: a hold is not a payment."""
        with pytest.raises(MandateRejected) as caught:
            capture_proof(
                payment_attempt_id="pa_1",
                provider_payment_id="pay_1",
                state=state,
                captured=Money(39800, "INR"),
                evidence_source="WEBHOOK",
            )
        assert caught.value.reason == "payment_receipt_requires_verified_capture"

    def test_a_browser_callback_can_never_ground_a_payment_receipt(self) -> None:
        """ADR 0003 D8. The buyer coming back is not evidence that money moved."""
        with pytest.raises(MandateRejected) as caught:
            capture_proof(
                payment_attempt_id="pa_1",
                provider_payment_id="pay_1",
                state=PaymentState.CAPTURED,
                captured=Money(39800, "INR"),
                evidence_source="BROWSER_CALLBACK",
            )
        assert caught.value.reason == "capture_evidence_source_is_not_authoritative"

    def test_issue_payment_receipt_cannot_be_called_without_a_proof(self) -> None:
        """The structural half of the property: absence by construction.

        ``issue_payment_receipt`` has no default for ``proof`` and no overload that omits
        it, so there is no call that skips the check -- the guard is the signature rather
        than a branch inside the body.
        """
        import inspect

        signature = inspect.signature(issue_payment_receipt)
        proof = signature.parameters["proof"]
        assert proof.default is inspect.Parameter.empty
        assert proof.kind is inspect.Parameter.KEYWORD_ONLY

    def test_a_captured_payment_yields_a_verifiable_receipt(
        self, platform_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        proof = capture_proof(
            payment_attempt_id="pa_1",
            provider_payment_id="pay_abc123",
            state=PaymentState.CAPTURED,
            captured=Money(39800, "INR"),
            evidence_source="WEBHOOK",
        )
        receipt = issue_payment_receipt(
            issuer="demo.invalid",
            reference="ref-1",
            proof=proof,
            signer=platform_signer,
            issued_at=1_764_000_000,
        )
        payload = verify_receipt(receipt, ring, expected_reference="ref-1")
        assert payload["status"] == "Success"
        assert payload["payment_id"] == "pay_abc123"
        assert payload["iat"] == 1_764_000_000


class TestReceipts:
    def test_a_checkout_receipt_verifies_against_its_reference(
        self, platform_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        receipt = issue_checkout_receipt(
            issuer="demo.invalid",
            reference="ref-checkout",
            order_id="ord_1",
            signer=platform_signer,
            issued_at=1_764_000_000,
        )
        payload = verify_receipt(receipt, ring, expected_reference="ref-checkout")
        assert payload["status"] == "Success"
        assert payload["order_id"] == "ord_1"

    def test_a_genuine_receipt_for_another_mandate_is_refused(
        self, platform_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """The check the SDK's own verifier omits.

        This receipt is real, correctly signed, and settles somebody else's order. A
        verifier that only checked the signature would accept it.
        """
        receipt = issue_checkout_receipt(
            issuer="demo.invalid",
            reference="ref-someone-else",
            order_id="ord_2",
            signer=platform_signer,
            issued_at=1_764_000_000,
        )
        with pytest.raises(MandateRejected) as caught:
            verify_receipt(receipt, ring, expected_reference="ref-mine")
        assert caught.value.reason == "receipt_settles_a_different_mandate"

    def test_a_rejection_receipt_is_signed_and_verifiable(
        self, platform_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        """A signed refusal lets the other party prove what they were told."""
        receipt = issue_rejection_receipt(
            issuer="demo.invalid",
            reference="ref-1",
            error="mandate_not_valid",
            error_description="The presented checkout mandate had expired.",
            signer=platform_signer,
            issued_at=1_764_000_000,
        )
        payload = verify_receipt(receipt, ring, expected_reference="ref-1")
        assert payload["status"] == "Error"
        assert payload["error"] == "mandate_not_valid"

    def test_a_receipt_signed_by_an_untrusted_key_is_refused(
        self, cp_foreign_key: JWK, ring: KeyRing
    ) -> None:
        stranger = InProcessSigner.from_jwk(cp_foreign_key)
        receipt = issue_checkout_receipt(
            issuer="attacker.invalid",
            reference="ref-1",
            order_id="ord_1",
            signer=stranger,
            issued_at=1_764_000_000,
        )
        with pytest.raises(SignatureRejected) as caught:
            verify_receipt(receipt, ring, expected_reference="ref-1")
        assert caught.value.reason == "unknown_kid"

    def test_a_receipt_without_a_reference_is_refused(
        self, platform_signer: InProcessSigner, ring: KeyRing
    ) -> None:
        malformed = platform_signer.sign({"typ": "JWT"}, b'{"status":"Success","iss":"x"}')
        with pytest.raises(SchemaRejected) as caught:
            verify_receipt(malformed, ring, expected_reference="ref-1")
        assert caught.value.reason == "receipt_carries_no_reference"
