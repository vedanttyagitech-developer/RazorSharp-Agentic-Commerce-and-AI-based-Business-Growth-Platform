"""Test-only issuer. Production Kernel never owns this key."""

import json
import time

from commerce_domain import canonicalize, uuid7
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS
from transaction_kernel.reserve_proofs import AUDIENCE, ISSUER, TYPE

KEY = JWK.generate(kty="EC", crv="P-256", kid="kernel-reserve-test")
PUBLIC = json.dumps({"keys": [KEY.export_public(as_dict=True)]})


def sign(bounds):
    claims = dict(
        bounds,
        version=1,
        iss=ISSUER,
        aud=AUDIENCE,
        issued_at=int(time.time()),
        jti=str(uuid7()),
        nonce=str(uuid7()),
        initial_epoch=0,
        provider_reference="sim_reserve_test",
    )
    token = JWS(canonicalize(claims))
    token.add_signature(KEY, protected={"alg": "ES256", "kid": KEY["kid"], "typ": TYPE})
    return token.serialize(compact=True)
