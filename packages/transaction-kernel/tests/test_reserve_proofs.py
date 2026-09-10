"""Adversarial signature profile tests independent of database state."""

import json
import time

import pytest
from commerce_domain import canonicalize, uuid7
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS
from reserve_signing_support import KEY, PUBLIC, sign
from transaction_kernel.reserve_proofs import TYPE, ReserveProofError, bound_claims, verify_artifact


def bounds():
    return bound_claims(
        authority_id=uuid7(),
        tenant_id=uuid7(),
        merchant_id=uuid7(),
        buyer_ref="test-buyer",
        currency="INR",
        max_amount_minor=500000,
        per_purchase_limit_minor=20000,
        allowed_skus=None,
        expires_at=None,
    )


def test_good_signature_and_rotation():
    token = sign(bounds())
    other = JWK.generate(kty="EC", crv="P-256", kid="next-key")
    ring = json.loads(PUBLIC)
    ring["keys"].append(other.export_public(as_dict=True))
    assert verify_artifact(token, jwks=json.dumps(ring))["max_amount_minor"] == 500000


@pytest.mark.parametrize("change", ["unknown", "revoked", "private", "duplicate", "wrong-key"])
def test_untrusted_keys_refused(change):
    ring = json.loads(PUBLIC)
    key = ring["keys"][0]
    if change == "unknown":
        key["kid"] = "unrelated"
    elif change == "revoked":
        ring["revoked_kids"] = [key["kid"]]
    elif change == "private":
        ring["keys"] = [KEY.export_private(as_dict=True)]
    elif change == "duplicate":
        ring["keys"].append(dict(key))
    else:
        ring["keys"] = [
            JWK.generate(kty="EC", crv="P-256", kid=key["kid"]).export_public(as_dict=True)
        ]
    with pytest.raises(ReserveProofError):
        verify_artifact(sign(bounds()), jwks=json.dumps(ring))


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "bank"),
        ("aud", "another-system"),
        ("version", True),
        ("initial_epoch", False),
        ("issued_at", int(time.time()) + 3600),
        ("max_amount_minor", True),
        ("per_purchase_limit_minor", 600000),
        ("allowed_skus", ["B", "A"]),
        ("expires_at", "2030-01-01"),
        ("unexpected", 1),
    ],
)
def test_invalid_signed_claims_refused(field, value):
    claims = verify_artifact(sign(bounds()), jwks=PUBLIC)
    claims[field] = value
    token = JWS(canonicalize(claims))
    token.add_signature(KEY, protected={"alg": "ES256", "kid": KEY["kid"], "typ": TYPE})
    with pytest.raises(ReserveProofError):
        verify_artifact(token.serialize(compact=True), jwks=PUBLIC)


def test_payload_tampering_without_resigning_refused():
    import base64

    token = sign(bounds()).split(".")
    claims = json.loads(base64.urlsafe_b64decode(token[1] + "=="))
    claims["max_amount_minor"] = 9000000
    token[1] = base64.urlsafe_b64encode(canonicalize(claims)).decode().rstrip("=")
    with pytest.raises(ReserveProofError):
        verify_artifact(".".join(token), jwks=PUBLIC)


def test_unsigned_token_refused():
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    with pytest.raises(ReserveProofError):
        verify_artifact(header + ".e30.", jwks=PUBLIC)


@pytest.mark.parametrize("algorithm", ["HS256", "EdDSA"])
def test_other_algorithm_profiles_refused(algorithm):
    key = (
        JWK.generate(kty="oct", size=256, kid=KEY["kid"])
        if algorithm == "HS256"
        else JWK.generate(kty="OKP", crv="Ed25519", kid=KEY["kid"])
    )
    token = JWS(canonicalize(verify_artifact(sign(bounds()), jwks=PUBLIC)))
    token.add_signature(key, protected={"alg": algorithm, "kid": KEY["kid"], "typ": TYPE})
    with pytest.raises(ReserveProofError):
        verify_artifact(token.serialize(compact=True), jwks=PUBLIC)
