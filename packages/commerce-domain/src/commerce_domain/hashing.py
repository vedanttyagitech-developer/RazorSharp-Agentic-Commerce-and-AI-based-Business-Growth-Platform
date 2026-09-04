"""Hashes over canonical bytes.

Base64url-without-padding is the canonical encoding, matching the AP2 and JWS
conventions this platform must interoperate with. Hex is offered for logs and
human-facing evidence only.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from .jcs import canonicalize


def b64url(raw: bytes) -> str:
    """Base64url encode without padding, per RFC 7515."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    """Inverse of :func:`b64url`, restoring stripped padding."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sha256_b64url(raw: bytes) -> str:
    return b64url(hashlib.sha256(raw).digest())


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_hash(value: Any) -> str:
    """The platform's canonical content hash: base64url SHA-256 over JCS bytes.

    This is the value bound into an approval, quoted in the trusted approval card and
    compared at admission. If this function changes, every stored approval becomes
    unverifiable -- treat it as a frozen contract and version it if it must ever move.
    """
    return sha256_b64url(canonicalize(value))
