"""Canonical commerce domain primitives.

Deterministic by construction: no I/O, no clock beyond explicit arguments, no model call.
Everything the Transaction Assurance Kernel hashes or compares originates here.
"""

from .errors import CanonicalizationError, CurrencyMismatchError, DomainError, MoneyError
from .hashing import b64url, b64url_decode, canonical_hash, sha256_b64url, sha256_hex
from .ids import uuid7, uuid7_str
from .jcs import canonicalize, canonicalize_str
from .money import Money, exponent_for

__all__ = [
    "CanonicalizationError",
    "CurrencyMismatchError",
    "DomainError",
    "Money",
    "MoneyError",
    "b64url",
    "b64url_decode",
    "canonical_hash",
    "canonicalize",
    "canonicalize_str",
    "exponent_for",
    "sha256_b64url",
    "sha256_hex",
    "uuid7",
    "uuid7_str",
]
