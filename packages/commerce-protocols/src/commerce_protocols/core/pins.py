"""The pinned protocol matrix, specification 13.2.

A protocol adapter that accepts whatever version the caller announces is not an adapter,
it is an attack surface. Every version this platform will answer to is named here, once,
as data; :func:`require_pin` is the only way an adapter learns whether it may proceed.

The matrix is deliberately closed. A caller announcing ``UCP/2027-01-01`` is refused even
if that version turns out to be a superset of the one we implement, because the fixtures
that prove conformance were recorded against the pinned version and nothing has proved
anything about the other one. Specification 13.3 puts it plainly: reading a specification
is not conformance, the vector is.

``ClaimBoundary`` exists because "implemented" and "approved by the platform that owns the
protocol" are different facts, and the specification is emphatic that the second must
never be claimed on the strength of the first (13.2, 16.2). The boundary travels with the
pin so that any surface reporting protocol status -- the inspector, the status table, a
panel answer -- reads it from the same place rather than from someone's memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "PINS",
    "ClaimBoundary",
    "Protocol",
    "ProtocolPin",
    "UnsupportedProtocolError",
    "UnsupportedVersionError",
    "require_pin",
]


class Protocol(StrEnum):
    """The protocols this platform speaks. ``x402`` is deliberately absent (13.2)."""

    UCP = "UCP"
    AP2 = "AP2"
    ACP = "ACP"
    MCP = "MCP"


class ClaimBoundary(StrEnum):
    """How far a claim about a protocol may honestly go.

    ``LOCAL_CONFORMANCE`` is the strongest thing this project may say on its own: the
    interface validates against the pinned public schema in tests we run. Saying more --
    that a merchant is live in an external platform's checkout -- depends on decisions
    made by that platform's ecosystem and cannot be earned by writing code here.
    """

    #: Validated locally against the pinned public schema and fixtures.
    LOCAL_CONFORMANCE = "LOCAL_CONFORMANCE"
    #: Interface exists and is exercised, but external onboarding is out of scope.
    COMPATIBLE_INTERFACE = "COMPATIBLE_INTERFACE"
    #: Alignment described against public information only; no schema is invented.
    PUBLIC_INFORMATION_ALIGNMENT = "PUBLIC_INFORMATION_ALIGNMENT"


@dataclass(frozen=True, slots=True)
class ProtocolPin:
    """One row of the specification 13.2 matrix.

    ``source_ref`` is a commit, a dated schema release or an API-version header value --
    whatever uniquely identifies the bytes the fixtures were recorded against. It is a
    string rather than a structured type because the four protocols pin themselves in
    four different vocabularies, and flattening them into a common shape would invent a
    precision none of them have.
    """

    protocol: Protocol
    version: str
    source_ref: str
    boundary: ClaimBoundary
    #: What this platform must never assert about the protocol, in prose, so that the
    #: sentence a person would be tempted to write is refuted at the point of definition.
    disclaimer: str


#: Every version this platform answers to. Keyed by protocol; one pin each in P0.
PINS: Final[dict[Protocol, ProtocolPin]] = {
    Protocol.UCP: ProtocolPin(
        protocol=Protocol.UCP,
        version="2026-08-25",
        source_ref="2026-08-25",
        boundary=ClaimBoundary.LOCAL_CONFORMANCE,
        disclaimer=(
            "Implementing UCP does not make a merchant available inside Gemini or Google "
            "AI Mode; that requires separate platform onboarding."
        ),
    ),
    Protocol.AP2: ProtocolPin(
        protocol=Protocol.AP2,
        version="v0.2.0",
        source_ref="b4587ac1d055888a73b4b21750973cffba961793",
        boundary=ClaimBoundary.LOCAL_CONFORMANCE,
        disclaimer=(
            "The accepted cryptographic profile is narrowed to ES256 by this project. "
            "AP2 itself permits a broader profile; the narrowing is our decision."
        ),
    ),
    Protocol.ACP: ProtocolPin(
        protocol=Protocol.ACP,
        version="2026-04-17",
        source_ref="API-Version: 2026-04-17",
        boundary=ClaimBoundary.COMPATIBLE_INTERFACE,
        disclaimer=(
            "An ACP-compatible interface validated locally against the pinned public "
            "schema is not 'live in ChatGPT Instant Checkout'. Product-feed acceptance, "
            "merchant approval and distribution remain controlled by OpenAI."
        ),
    ),
    Protocol.MCP: ProtocolPin(
        protocol=Protocol.MCP,
        version="2025-06-18",
        source_ref="2025-06-18",
        boundary=ClaimBoundary.COMPATIBLE_INTERFACE,
        disclaimer=(
            "Claude can consume these governed tools through MCP, but Claude is not part "
            "of the tested runtime."
        ),
    ),
}


class UnsupportedProtocolError(ValueError):
    """A protocol this platform does not speak at all."""


class UnsupportedVersionError(ValueError):
    """A protocol we speak, announced at a version we have proved nothing about.

    Carries both versions so an adapter can put the supported one in its refusal; a
    caller that learns which version to use recovers on its next request, and telling it
    discloses nothing an interoperability profile would not already publish.
    """

    def __init__(self, protocol: Protocol, announced: str, supported: str) -> None:
        super().__init__(
            f"{protocol} version {announced!r} is not pinned; this platform implements "
            f"{supported!r} and refuses versions it has no fixtures for"
        )
        self.protocol = protocol
        self.announced = announced
        self.supported = supported


def require_pin(protocol: Protocol, version: str | None) -> ProtocolPin:
    """The pin for ``protocol``, or refuse.

    ``version`` of ``None`` means the caller announced nothing. That is refused rather
    than defaulted to the pinned version: a caller that does not state which contract it
    believes it is speaking has not agreed to one, and silently choosing on its behalf is
    how a future version change becomes a silent behaviour change for somebody's
    integration.
    """
    pin = PINS.get(protocol)
    if pin is None:  # pragma: no cover - Protocol is closed; defensive only
        raise UnsupportedProtocolError(f"{protocol!r} is not a protocol this platform speaks")
    if version != pin.version:
        raise UnsupportedVersionError(protocol, version or "<absent>", pin.version)
    return pin
