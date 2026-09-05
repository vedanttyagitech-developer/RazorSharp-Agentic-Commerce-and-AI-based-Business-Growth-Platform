"""The UCP business profile and its published keys, specification 14.1.

A business profile is how a merchant tells the world which protocol version it speaks, what
it can do, and -- crucially -- which public keys will verify the signatures it produces. It
is served from a well-known location so that a counterparty can discover it without prior
arrangement.

The profile is the trust anchor for everything else in this package. A peer verifying a
detached merchant authorization resolves the ``kid`` from its protected header against the
keys published here. So two properties matter more than the rest of the document:

**Only public halves are ever published.** :func:`profile_document` builds its JWK Set from
:meth:`commerce_protocols.ap2.signing.KeyRing.public_jwks`, which is the one export path in
this package and exports public material only. The test suite asserts over the serialised
bytes rather than over the structure, because the failure being guarded against is a private
key reaching a public URL and no amount of structural correctness would make that survivable.

**Rotation is additive.** Specification 14.1 requires keys to rotate "without silently
invalidating stored evidence". A profile that published only the current signing key would,
the moment it rotated, make every previously issued merchant authorization unverifiable --
and unverifiable in the worst possible way, as an ordinary bad-signature result that looks
like tampering. So the published set is the whole ring: the key currently being signed with,
plus every retired key whose signatures are still being checked. Retiring a key means
removing it from the signer, not from the profile.

Versioning
----------
The profile carries its own ``profile_version``, which increments whenever the document
changes -- a key added, a capability added, the protocol pin moved. A counterparty that
caches the profile can tell whether what it holds is current without diffing the document,
and an operator looking at two deployments can tell whether they are serving the same thing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ..ap2.signing import KeyRing
from ..core.pins import PINS, ClaimBoundary, Protocol

__all__ = [
    "MERCHANT_PROFILE_PATH",
    "PLATFORM_PROFILE_PATH",
    "SUPPORTED_CAPABILITIES",
    "BusinessProfile",
    "jwks_document",
    "profile_document",
]

#: Where a merchant's profile is served. A well-known path so a counterparty can find it
#: without prior arrangement, which is the entire point of publishing one.
MERCHANT_PROFILE_PATH: Final[str] = "/.well-known/ucp/business-profile"

#: The demo platform's own profile. Specification 14.1 asks for the platform's verification
#: keys to be published separately from the merchant's, and separately is the operative
#: word: "the merchant signed this checkout" and "the platform signed this receipt" are
#: different claims, and they stop being different if one key set covers both.
PLATFORM_PROFILE_PATH: Final[str] = "/.well-known/ucp/platform-profile"

#: What this implementation can actually do. Deliberately not a copy of everything UCP
#: defines: a profile is a promise, and advertising a capability the platform does not
#: implement is how a counterparty builds against something that will fail in production.
#: Payment completion is absent, and its absence is the honest statement of specification
#: 14.3 -- there is no negotiated headless payment Action here.
SUPPORTED_CAPABILITIES: Final[tuple[str, ...]] = (
    "catalogue.read",
    "basket.write",
    "checkout.create",
    "checkout.read",
    "checkout.complete.requires_escalation",
    "order.read",
    "order.cancel.propose",
    "refund.propose",
)


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    """One merchant's or platform's published identity, capabilities and keys."""

    profile_version: int
    subject_id: str
    display_name: str
    website: str
    ring: KeyRing
    capabilities: Sequence[str] = SUPPORTED_CAPABILITIES

    def __post_init__(self) -> None:
        if self.profile_version < 1:
            raise ValueError("profile_version starts at 1 and only ever increases")
        if not self.ring.keys:
            raise ValueError(
                "a business profile with no verification keys is a profile nobody can "
                "check a signature against"
            )


def jwks_document(ring: KeyRing) -> dict[str, Any]:
    """The JWK Set a counterparty fetches to verify this party's signatures.

    Every key carries a stable ``kid``, which :class:`KeyRing` already guarantees is unique
    within the ring -- so a ``kid`` in a protected header resolves to exactly one key, and
    "who signed this" has one answer.
    """
    return ring.public_jwks()


def profile_document(profile: BusinessProfile) -> dict[str, Any]:
    """The published profile, as the JSON a well-known endpoint serves.

    The claim boundary and disclaimer travel *inside the document*, which is unusual and
    deliberate. Specification 13.2 and 16.2 both insist that "implemented" must never be
    presented as "approved by the platform that owns the protocol", and the most durable
    place to keep that qualification is beside the claim it qualifies -- where a
    counterparty reads it, rather than in a status table somebody has to remember to consult.
    """
    pin = PINS[Protocol.UCP]
    return {
        "profile_version": profile.profile_version,
        "protocol": pin.protocol.value,
        "protocol_version": pin.version,
        "id": profile.subject_id,
        "name": profile.display_name,
        "website": profile.website,
        "capabilities": list(profile.capabilities),
        "jwks": jwks_document(profile.ring),
        "conformance": {
            "boundary": pin.boundary.value,
            "verified_locally": pin.boundary is ClaimBoundary.LOCAL_CONFORMANCE,
            "disclaimer": pin.disclaimer,
        },
    }
