"""Canonical commerce domain primitives, and the vocabulary every plane shares.

Deterministic by construction: no I/O, no clock beyond explicit arguments, no model call.
Everything the Transaction Trust Kernel hashes or compares originates here.

Two kinds of thing live here, and the second arrived later. The first is arithmetic and
encoding -- money, canonical JSON, hashes, identifiers -- which the kernel needs in order to
be deterministic. The second is the neutral contracts: who is asking, which checkout version,
what moved, what was decided. Those were kernel types until the packages that only *read*
them had to declare a dependency on the package that *decides*, which put every financial
write one import line away from a model-driven runtime. They are here so that a component
which must never be able to act can still be able to explain.

This package depends on nothing of ours. That is the property that makes it a safe place to
put a shared name, and it is worth defending on every future addition.
"""

from .contracts import (
    REQUIRED_POLICY_KINDS,
    ActorType,
    AdmissionDecision,
    AgentPrincipal,
    CheckoutRef,
    Delta,
    PolicyKind,
)
from .errors import CanonicalizationError, CurrencyMismatchError, DomainError, MoneyError
from .hashing import b64url, b64url_decode, canonical_hash, sha256_b64url, sha256_hex
from .ids import (
    ParsedOrderReference,
    ReferenceFormatError,
    order_reference,
    parse_order_reference,
    uuid7,
    uuid7_str,
)
from .jcs import canonicalize, canonicalize_str
from .money import Money, exponent_for
from .recovery import NEEDS_REAPPROVAL, NOT_A_SUCCESS, RETRYABLE, RecoveryCode

__all__ = [
    "NEEDS_REAPPROVAL",
    "NOT_A_SUCCESS",
    "REQUIRED_POLICY_KINDS",
    "RETRYABLE",
    "ActorType",
    "AdmissionDecision",
    "AgentPrincipal",
    "CheckoutRef",
    "Delta",
    "PolicyKind",
    "RecoveryCode",
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
    "ParsedOrderReference",
    "ReferenceFormatError",
    "order_reference",
    "parse_order_reference",
    "uuid7",
    "uuid7_str",
]
