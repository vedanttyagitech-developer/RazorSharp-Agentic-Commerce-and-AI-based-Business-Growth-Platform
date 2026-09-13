"""Cloud KMS ES256 adapter. Locally verify every DER signature before returning raw JWS."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import rfc8785
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from jwcrypto.jwk import JWK


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class KmsSigner:
    def __init__(self, transport: Any, version: str, kid: str, fingerprint: str):
        if not re.fullmatch(
            r"projects/[\w-]+/locations/[\w-]+/keyRings/[\w-]+/"
            r"cryptoKeys/[\w-]+/cryptoKeyVersions/[1-9][0-9]*",
            version,
        ):
            raise ValueError("KMS key must name an immutable version")
        self.transport = transport
        self.version = version
        self.kid = kid
        self.url = "https://cloudkms.googleapis.com/v1/" + version
        response = transport.get(self.url + "/publicKey", timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("algorithm") != "EC_SIGN_P256_SHA256" or data.get("protectionLevel") != "HSM":
            raise ValueError("HSM-protected EC_SIGN_P256_SHA256 key required")
        if data.get("algorithm") != "EC_SIGN_P256_SHA256":
            raise ValueError("KMS signing algorithm must be EC_SIGN_P256_SHA256")
        public = serialization.load_pem_public_key(data["pem"].encode())
        if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(
            public.curve, ec.SECP256R1
        ):
            raise ValueError("KMS public key must be P-256")
        self.public: ec.EllipticCurvePublicKey = public
        self.jwk = JWK.from_pem(data["pem"].encode()).export_public(as_dict=True)
        self.jwk.update(kid=kid, alg="ES256", use="sig")
        if JWK(**self.jwk).thumbprint() != fingerprint:
            raise ValueError("KMS key does not match operator-approved fingerprint")

    def sign(self, claims: dict[str, Any]) -> str:
        from reserve_trust import TYPE

        signing_input = (
            b64(rfc8785.dumps({"alg": "ES256", "kid": self.kid, "typ": TYPE}))
            + "."
            + b64(rfc8785.dumps(claims))
        ).encode()
        digest = hashlib.sha256(signing_input).digest()
        response = self.transport.post(
            self.url + ":asymmetricSign",
            json={"digest": {"sha256": base64.b64encode(digest).decode()}},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("name") != self.version:
            raise ValueError("KMS returned a different key version")
        der = base64.b64decode(result["signature"], validate=True)
        # This also detects corrupt requests/responses and DER-vs-JOSE mistakes.
        self.public.verify(der, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        r, s = utils.decode_dss_signature(der)
        return signing_input.decode() + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
