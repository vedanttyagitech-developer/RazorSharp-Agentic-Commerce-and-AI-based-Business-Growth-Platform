import copy
import time

import pytest
from jwcrypto.jwk import JWK
from reserve_trust import AUDIENCE, ISSUER, TrustRejectedError, verify_key


def configured():
    key = JWK.generate(kty="EC", crv="P-256", kid="first").export_public(as_dict=True)
    now = int(time.time())
    policy = {
        "version": 1,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_uri": "https://issuer.example/.well-known/reserve/jwks",
        "issued_at": now - 1,
        "expires_at": now + 3600,
        "keys": {"first": {"sha256": JWK(**key).thumbprint(), "status": "active"}},
    }
    return key, policy


def test_key_substitution_same_kid_is_rejected():
    key, policy = configured()
    verify_key(policy, key)
    attacker = JWK.generate(kty="EC", crv="P-256", kid="first").export_public(as_dict=True)
    with pytest.raises(TrustRejectedError, match="fingerprint"):
        verify_key(policy, attacker)


@pytest.mark.parametrize("change", ["expiry", "future", "revoked", "issuer", "audience", "http"])
def test_stale_or_untrusted_policy_fails_closed(change):
    key, policy = configured()
    if change == "expiry":
        policy["expires_at"] = int(time.time()) - 1
    elif change == "future":
        policy["issued_at"] = int(time.time()) + 30
    elif change == "revoked":
        policy["keys"]["first"]["status"] = "revoked"
    elif change in ("issuer", "audience"):
        policy[change] = "attacker"
    else:
        policy["jwks_uri"] = "http://issuer.example/.well-known/reserve/jwks"
    with pytest.raises(TrustRejectedError):
        verify_key(policy, key)


def test_rotation_requires_operator_pin_and_revocation_survives_jwks_claims():
    old, policy = configured()
    new = JWK.generate(kty="EC", crv="P-256", kid="next").export_public(as_dict=True)
    with pytest.raises(TrustRejectedError):
        verify_key(policy, new)
    rotated = copy.deepcopy(policy)
    rotated["keys"]["next"] = {"sha256": JWK(**new).thumbprint(), "status": "active"}
    verify_key(rotated, old)
    verify_key(rotated, new)
    rotated["keys"]["first"]["status"] = "revoked"
    with pytest.raises(TrustRejectedError):
        verify_key(rotated, old)
