#!/usr/bin/env python3
"""Enroll one independently authenticated public key. Never enroll from an artifact.

Supply the fingerprint verified through the issuer's authenticated administrative channel.
For rotation preserve existing active pins; use --revoke-kid for immediate withdrawal.
The output is public configuration; only the security operator should have write access.
"""

import argparse
import json
import os
import time
from pathlib import Path

from jwcrypto.jwk import JWK
from reserve_trust import AUDIENCE, ISSUER, validate_policy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-jwk", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--jwks-uri", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--valid-for-seconds", type=int, default=3600)
    parser.add_argument("--revoke-kid", action="append", default=[])
    args = parser.parse_args()
    key = json.loads(args.public_jwk.read_text())
    if "d" in key or key.get("kty") != "EC" or key.get("crv") != "P-256":
        raise SystemExit("A public P-256 JWK is required")
    fingerprint = JWK(**key).thumbprint()
    if fingerprint != args.expected_sha256:
        raise SystemExit("Key does not match independently authenticated fingerprint")
    pins = {}
    if args.output.exists():
        old = json.loads(args.output.read_text())
        if (
            old["issuer"] != ISSUER
            or old["audience"] != AUDIENCE
            or old["jwks_uri"] != args.jwks_uri
        ):
            raise SystemExit("Existing issuer/endpoint cannot be silently replaced")
        pins = old["keys"]
    existing = pins.get(key["kid"])
    if existing and (existing["status"] == "revoked" or existing["sha256"] != fingerprint):
        raise SystemExit("Do not reuse a revoked kid or replace its key material")
    pins[key["kid"]] = {"sha256": fingerprint, "status": "active"}
    for kid in args.revoke_kid:
        if kid not in pins:
            raise SystemExit("Cannot revoke a key absent from the trust inventory")
        pins[kid]["status"] = "revoked"
    now = int(time.time())
    policy = {
        "version": 1,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_uri": args.jwks_uri,
        "issued_at": now,
        "expires_at": now + args.valid_for_seconds,
        "keys": pins,
    }
    validate_policy(policy)
    temp = args.output.with_name(args.output.name + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(policy, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, args.output)
    finally:
        temp.unlink(missing_ok=True)
    print("Trust policy provisioned; no private key was read or written.")


if __name__ == "__main__":
    main()
