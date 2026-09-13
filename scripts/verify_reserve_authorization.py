#!/usr/bin/env python3
"""Offline Reserve simulator verifier. No application imports, network or private keys.

Usage: python verify_reserve_authorization.py proof.json --trusted-jwks trusted-keys.json
Dependencies: jwcrypto, rfc8785 (available in the workspace .venv).
Trust keys through an independent trusted channel; a key included by an attacker alongside
an artifact proves nothing. This command never establishes live capacity or revocation.
"""

import argparse
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rfc8785
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS


def verify(
    document: dict[str, Any], trust: dict[str, Any], policy: dict[str, Any] | None = None
) -> dict[str, Any]:
    compact = document["artifact_jws"]
    if not isinstance(compact, str) or len(compact) > 32768 or compact.count(".") != 2:
        raise ValueError("Expected a compact JWS")
    jws = JWS()
    jws.allowed_algs = ["ES256"]
    jws.deserialize(compact)
    header = jws.jose_header
    if (
        set(header) != {"alg", "kid", "typ"}
        or header["alg"] != "ES256"
        or header["typ"] != "reserve-authority+jws"
    ):
        raise ValueError("Unsupported signature profile")
    keys = [k for k in trust["keys"] if k.get("kid") == header["kid"]]
    if len(keys) != 1 or header["kid"] in trust.get("revoked_kids", []):
        raise ValueError("Signer is unknown, ambiguous or revoked in the supplied trust set")
    key = keys[0]
    if "d" in key or key.get("kty") != "EC" or key.get("crv") != "P-256":
        raise ValueError("Supply public P-256 keys only")
    if policy is not None:
        from reserve_trust import verify_key

        verify_key(policy, key)
    jws.verify(JWK(**key), alg="ES256")
    claims = json.loads(jws.payload)
    if rfc8785.dumps(claims) != jws.payload:
        raise ValueError("Payload is not canonical JSON")
    if (
        claims.get("iss") != "razorsharp-reserve-provider-simulator"
        or claims.get("aud") != "razorsharp-reserve"
        or type(claims.get("version")) is not int
        or claims["version"] not in (1, 2)
    ):
        raise ValueError("Unexpected issuer, audience or version")
    expected = {
        "version",
        "iss",
        "aud",
        "authority_id",
        "tenant_id",
        "merchant_id",
        "buyer_ref",
        "currency",
        "max_amount_minor",
        "per_purchase_limit_minor",
        "allowed_skus",
        "expires_at",
        "issued_at",
        "jti",
        "nonce",
        "provider_reference",
        "initial_epoch",
    }
    if claims["version"] == 2:
        expected.add("buyer_consent_sha256")
    if (
        set(claims) != expected
        or type(claims["initial_epoch"]) is not int
        or claims["initial_epoch"] != 0
    ):
        raise ValueError("Unexpected authority claim set or initial epoch")
    for name in ("authority_id", "tenant_id", "merchant_id", "jti", "nonce"):
        uuid.UUID(claims[name])
    scope = claims["allowed_skus"]
    if scope is not None and (
        not isinstance(scope, list)
        or not scope
        or any(not isinstance(s, str) or not s for s in scope)
        or scope != sorted(set(scope))
    ):
        raise ValueError("Invalid selected-product scope")
    expiry_status = "UNTIL_REVOKED"
    if claims["expires_at"] is not None:
        expiry = datetime.fromisoformat(claims["expires_at"])
        if expiry.tzinfo is None:
            raise ValueError("Expiry has no timezone")
        expiry_status = "EXPIRED" if expiry <= datetime.now(UTC) else "NOT_EXPIRED"
    for name in ("max_amount_minor", "per_purchase_limit_minor"):
        value = claims.get(name)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("Invalid integer spending bounds")
    if (
        claims["max_amount_minor"] is None
        or (claims["per_purchase_limit_minor"] or 0) > claims["max_amount_minor"]
    ):
        raise ValueError("Invalid capacity")
    consent = "NOT_PRESENT_LEGACY"
    if claims["version"] == 2:
        evidence = document.get("consent_evidence")
        if not isinstance(evidence, dict) or hashlib.sha256(
            rfc8785.dumps(evidence)
        ).hexdigest() != claims.get("buyer_consent_sha256"):
            raise ValueError("Buyer consent evidence missing or changed")
        terms = {
            k: claims[k]
            for k in (
                "tenant_id",
                "merchant_id",
                "buyer_ref",
                "currency",
                "expires_at",
                "allowed_skus",
                "per_purchase_limit_minor",
            )
        }
        terms["capacity_minor"] = claims["max_amount_minor"]
        if hashlib.sha256(rfc8785.dumps(terms)).hexdigest() != evidence.get("terms_sha256"):
            raise ValueError("Consent terms do not match signed spending bounds")
        if evidence.get("user_verified") is not True:
            raise ValueError("Issuer has not attested verified buyer consent")
        consent = "ISSUER_ATTESTED_AND_HASH_BOUND"
    return {
        "signature": "VALID",
        "issuer_kind": "SIMULATOR",
        "key_id": header["kid"],
        "payload_sha256": hashlib.sha256(jws.payload).hexdigest(),
        "signed_terms": claims,
        "buyer_consent": consent,
        "permission_expiry": expiry_status,
        "spend_authorization": "NOT_ESTABLISHED_OFFLINE",
        "live_revocation": "NOT_CHECKED",
        "remaining_capacity": "NOT_CHECKED",
        "bank_authorization": "NOT_ESTABLISHED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proof", type=Path)
    parser.add_argument("--trusted-jwks", type=Path, required=True)
    parser.add_argument(
        "--trust-config", type=Path, help="Independently provisioned issuer/key pins"
    )
    args = parser.parse_args()
    try:
        result = verify(
            json.loads(args.proof.read_text()),
            json.loads(args.trusted_jwks.read_text()),
            None if args.trust_config is None else json.loads(args.trust_config.read_text()),
        )
    except Exception as exc:
        print(json.dumps({"signature": "REJECTED", "reason": str(exc)}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
