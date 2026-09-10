#!/usr/bin/env python3
"""Provision a stable local simulator key into an ignored dotenv file, without printing it.

Run with .venv/bin/python scripts/configure_reserve_signer.py. Deployment secrets should
be provisioned separately; this command never configures a bank/NPCI signing identity.
"""

import argparse
import json
import os
from pathlib import Path

from jwcrypto.jwk import JWK


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    path = args.env_file
    content = path.read_text() if path.exists() else ""
    names = ("RESERVE_PROVIDER_SIGNING_JWK", "RESERVE_PROVIDER_VERIFICATION_JWKS")
    present = [
        any(line.strip().startswith(name + "=") for line in content.splitlines()) for name in names
    ]
    if any(present):
        if not all(present):
            raise SystemExit(
                "Incomplete signer configuration; preserve the existing key "
                "and repair the public trust set."
            )
        print("Reserve simulator signer already configured; preserved existing keys.")
        return
    key = JWK.generate(kty="EC", crv="P-256", kid="reserve-simulator-v1", alg="ES256", use="sig")
    values = (key.export_private(), json.dumps({"keys": [key.export_public(as_dict=True)]}))
    # Exclusive temporary file and atomic replace avoid partial credential writes.
    temporary = path.with_name(path.name + ".reserve-key.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(
                content.rstrip()
                + "\n\n# Local Reserve provider simulator, not NPCI/bank credentials.\n"
            )
            for name, value in zip(names, values, strict=True):
                out.write(f"{name}='{value}'\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print("Stable Reserve simulator key configured. No credentials printed.")


if __name__ == "__main__":
    main()
