"""Verifier-owned pins. Discovery distributes keys; it never enrolls an issuer or key."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jwcrypto.jwk import JWK

ISSUER = "razorsharp-reserve-provider-simulator"
AUDIENCE = "razorsharp-reserve"
TYPE = "reserve-authority+jws"
JWKS_PATH = "/.well-known/reserve/jwks"
MAX_TRUST_LIFETIME = 86400


class TrustRejectedError(ValueError):
    """No money authorization may rely on an invalid or stale trust policy."""


def load_policy(path: str) -> dict[str, Any]:
    try:
        file = Path(path)
        if file.stat().st_size > 65536:
            raise TrustRejectedError("trust_policy_too_large")
        policy: dict[str, Any] = json.loads(file.read_text())
        validate_policy(policy)
        return policy
    except (OSError, TypeError, KeyError, ValueError) as exc:
        raise TrustRejectedError("trust_policy_unavailable_or_invalid") from exc


def validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy, dict) or set(policy) != {
        "version",
        "issuer",
        "audience",
        "jwks_uri",
        "issued_at",
        "expires_at",
        "keys",
    }:
        raise TrustRejectedError("invalid_trust_policy")
    if (
        type(policy["version"]) is not int
        or policy["version"] != 1
        or policy["issuer"] != ISSUER
        or policy["audience"] != AUDIENCE
    ):
        raise TrustRejectedError("unapproved_issuer_or_audience")
    now = time.time()
    start, end = policy["issued_at"], policy["expires_at"]
    if (
        type(start) is not int
        or type(end) is not int
        or not start <= now < end
        or not 0 < end - start <= MAX_TRUST_LIFETIME
    ):
        raise TrustRejectedError("trust_policy_expired_or_invalid")
    uri = urlsplit(policy["jwks_uri"])
    if (
        uri.scheme != "https"
        or not uri.hostname
        or uri.username
        or uri.password
        or uri.query
        or uri.fragment
        or uri.path != JWKS_PATH
    ):
        raise TrustRejectedError("invalid_pinned_jwks_uri")
    entries = policy["keys"]
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 32:
        raise TrustRejectedError("invalid_pinned_keys")
    for kid, entry in entries.items():
        if (
            not isinstance(kid, str)
            or not 1 <= len(kid) <= 128
            or not isinstance(entry, dict)
            or set(entry) != {"sha256", "status"}
            or entry["status"] not in ("active", "revoked")
            or not isinstance(entry["sha256"], str)
            or len(entry["sha256"]) != 43
        ):
            raise TrustRejectedError("invalid_pinned_key")


def verify_key(policy: dict[str, Any], key: dict[str, Any]) -> None:
    validate_policy(policy)
    kid = key.get("kid")
    entry = policy["keys"].get(kid) if isinstance(kid, str) else None
    if entry is None or entry["status"] != "active":
        raise TrustRejectedError("signer_not_pinned_or_revoked")
    if (
        key.get("kty") != "EC"
        or key.get("crv") != "P-256"
        or "d" in key
        or key.get("alg", "ES256") != "ES256"
        or key.get("use", "sig") != "sig"
    ):
        raise TrustRejectedError("public_es256_key_required")
    if JWK(**key).thumbprint() != entry["sha256"]:
        raise TrustRejectedError("signer_fingerprint_mismatch")


def public_ring(ring: dict[str, Any]) -> dict[str, Any]:
    """Never publish private material, even under a malformed deployment configuration."""
    return {
        "keys": [JWK(**k).export_public(as_dict=True) for k in ring["keys"]],
        "revoked_kids": ring.get("revoked_kids", []),
    }
