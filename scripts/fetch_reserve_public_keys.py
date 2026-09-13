#!/usr/bin/env python3
"""Fetch only a pre-approved HTTPS endpoint and verify every returned key against local pins.

This command does not modify the trust policy or follow redirects. Unknown rotated keys
require operator enrollment first. Output is public JWKS suitable for deployment config.
"""

import argparse
import json

import httpx
from reserve_trust import load_policy, verify_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trust-config", required=True)
    args = parser.parse_args()
    policy = load_policy(args.trust_config)
    with (
        httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client,
        client.stream("GET", policy["jwks_uri"]) as response,
    ):
        response.raise_for_status()
        content = bytearray()
        for part in response.iter_bytes():
            content.extend(part)
            if len(content) > 65536:
                raise ValueError("JWKS exceeds size limit")
    ring = json.loads(content)
    seen = set()
    for key in ring["keys"]:
        verify_key(policy, key)
        if key["kid"] in seen:
            raise ValueError("Ambiguous key id")
        seen.add(key["kid"])
    if not seen:
        raise ValueError("Empty verification key set")
    print(json.dumps(ring))


if __name__ == "__main__":
    main()
