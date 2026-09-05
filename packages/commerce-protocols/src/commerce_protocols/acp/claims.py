"""The sentence this adapter is allowed to say about itself, checked rather than promised.

Specification 16.2 draws a line that nothing in the code would otherwise defend. This
project may say that the merchant has an ACP-compatible interface validated locally against
the pinned public schema. It may not say that the merchant is live in ChatGPT Instant
Checkout, because product-feed acceptance, merchant approval, external sandbox access and
distribution are decisions OpenAI makes and no amount of correct code here earns them.

A rule of that shape usually lives in a README and decays the first time somebody writes a
pitch deck at midnight. So it lives here as a function instead. Any surface that reports
this adapter's status -- a status endpoint, a generated README table, a demo script's
banner -- builds its sentence through :func:`assert_claim_permitted`, and a sentence that
crosses the line raises before it can be rendered. The honest claim is the one that is easy
to make, which is the only way a rule like this survives contact with a deadline.

The gate is deliberately blunt: it refuses a forbidden phrase even when the surrounding
prose negates it. "We are not live in ChatGPT" is a true sentence that this function still
refuses, and that is the right trade. A gate that tried to parse negation would be a gate
that could be talked around, and a boundary is not something to be clever about. Prose that
must quote the forbidden sentence in order to refute it belongs in
:attr:`~commerce_protocols.core.pins.ProtocolPin.disclaimer`, which is data the platform
publishes rather than a claim the platform makes -- and which is why
:data:`FORBIDDEN_CLAIM` below is a constant this module never passes through its own gate.

The pin is the source of truth for the boundary and the disclaimer. Nothing here restates
either of them; :func:`describe_surface` reads both out of ``core.pins`` so that a change to
the matrix changes every status report at once.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from ..core import PINS, ClaimBoundary, Protocol, ProtocolPin

__all__ = [
    "CLAIM_FOR_BOUNDARY",
    "FORBIDDEN_CLAIM",
    "FORBIDDEN_CLAIM_RULES",
    "PERMITTED_CLAIM",
    "OverclaimError",
    "SurfaceStatus",
    "assert_claim_permitted",
    "describe_surface",
]

#: Specification 16.2's permitted sentence, verbatim. This is the *ceiling* on what may be
#: said, not a sentence this surface has earned: it asserts validation against the pinned
#: public schema, which is precisely what
#: :attr:`~commerce_protocols.core.pins.ClaimBoundary.LOCAL_CONFORMANCE` means. ACP is
#: pinned at ``COMPATIBLE_INTERFACE``, so :func:`describe_surface` does not report this one;
#: see :data:`CLAIM_FOR_BOUNDARY`.
PERMITTED_CLAIM: Final[str] = (
    "The merchant has an ACP-compatible interface validated locally against the "
    "pinned public schema."
)

#: Specification 16.2's forbidden sentence, verbatim. Kept as data so a test can prove the
#: gate refuses the exact wording the specification names, rather than a paraphrase of it.
FORBIDDEN_CLAIM: Final[str] = "The merchant is live in ChatGPT Instant Checkout."


class OverclaimError(ValueError):
    """A status sentence claimed more than the pinned boundary permits.

    A ``ValueError`` rather than a :class:`~commerce_protocols.core.ProtocolRejection`
    because this is never an external party's fault. Nobody outside can make this platform
    describe itself; an overclaim is our own code or our own copy being wrong, which is a
    bug, and dressing it up as a caller's refusal would put it in the wrong triage queue.
    """

    def __init__(self, statement: str, rule: str) -> None:
        super().__init__(
            f"the {rule} rule refuses this statement: specification 16.2 permits "
            f"{PERMITTED_CLAIM!r} and no more. Offending text: {statement!r}"
        )
        self.statement = statement
        self.rule = rule


#: Runs of anything that is not a letter or a digit collapse to one space before matching,
#: so ``ChatGPT``, ``Chat-GPT`` and ``chat gpt`` are the same three ways of saying one thing
#: and a rule does not have to enumerate them.
_NOISE = re.compile(r"[^a-z0-9]+")


def _normalise(statement: str) -> str:
    return _NOISE.sub(" ", statement.lower()).strip()


#: Named rules, so a refusal says which line was crossed rather than only that one was.
#: Each pattern matches against the normalised form above.
FORBIDDEN_CLAIM_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("external_distribution", re.compile(r"\blive (in|on|inside|within|through) chat ?gpt\b")),
    ("external_product_surface", re.compile(r"\binstant checkout\b")),
    (
        "external_approval",
        re.compile(r"\b(approved|certified|onboarded|accepted|whitelisted) by open ?ai\b"),
    ),
    ("external_approval", re.compile(r"\bopen ?ai (approval|approved|certification|certified)\b")),
    ("merchant_liveness", re.compile(r"\bmerchant is live\b")),
    ("certification", re.compile(r"\bcertified\b")),
    (
        "external_availability",
        re.compile(r"\b(available|launched|shipping|rolled out) (to|for|in|on) chat ?gpt\b"),
    ),
)


#: What each boundary entitles this project to say, so that a status report cannot assert
#: more than the pin it is reporting. Specification 16.2 names the sentence at the top of
#: this table as permitted; it does not say it is permitted unconditionally, and it is the
#: word "validated" that has to be earned. UCP and AP2 earn it -- their fixtures were
#: recorded from vendored schema bundles -- and ACP does not: no ACP OpenAPI or JSON Schema
#: artifact exists anywhere in this repository to validate against, which is exactly why
#: specification 13.2 pins ACP one rung lower. Reading the sentence out of the boundary
#: rather than beside it is what keeps the two from drifting apart the next time somebody
#: summarises four protocols in one column.
CLAIM_FOR_BOUNDARY: Final[Mapping[ClaimBoundary, str]] = MappingProxyType(
    {
        ClaimBoundary.LOCAL_CONFORMANCE: PERMITTED_CLAIM,
        ClaimBoundary.COMPATIBLE_INTERFACE: (
            "The merchant has an ACP-compatible interface, exercised end to end against "
            "the pinned API version by a local external-buyer simulator."
        ),
        ClaimBoundary.PUBLIC_INFORMATION_ALIGNMENT: (
            "The merchant's interface is described against ACP's public information at the "
            "pinned API version; no schema has been validated against."
        ),
    }
)


def assert_claim_permitted(statement: str) -> str:
    """Return ``statement`` if specification 16.2 permits it; raise otherwise.

    Returns the statement so it can be used inline at the point of rendering --
    ``banner = assert_claim_permitted(f"ACP: {status}")`` -- which is what makes the gate
    hard to route around by accident. A checker that had to be called on a separate line
    is a checker somebody eventually forgets.
    """
    normalised = _normalise(statement)
    for rule, pattern in FORBIDDEN_CLAIM_RULES:
        if pattern.search(normalised):
            raise OverclaimError(statement, rule)
    return statement


@dataclass(frozen=True, slots=True)
class SurfaceStatus:
    """What this adapter is willing to say about itself, in one object.

    ``claim`` has already been through :func:`assert_claim_permitted`; ``disclaimer`` is the
    pin's refutation and has deliberately not been, for the reason the module docstring
    gives. Keeping both on one object means a status renderer cannot show the claim without
    having the refutation to hand, which is the whole point of publishing either.
    """

    protocol: Protocol
    version: str
    source_ref: str
    boundary: ClaimBoundary
    claim: str
    disclaimer: str

    def as_payload(self) -> dict[str, Any]:
        """A JSON-safe rendering for a status endpoint, an audit row or a README table."""
        return {
            "protocol": self.protocol.value,
            "protocol_version": self.version,
            "source_ref": self.source_ref,
            "claim_boundary": self.boundary.value,
            "claim": self.claim,
            "disclaimer": self.disclaimer,
        }


def describe_surface(pin: ProtocolPin | None = None) -> SurfaceStatus:
    """This adapter's honest status, read out of the pinned matrix.

    ``COMPATIBLE_INTERFACE`` is what specification 13.2 pins ACP at, and it is weaker than
    the ``LOCAL_CONFORMANCE`` that UCP and AP2 carry: those two validate against published
    schema bundles the fixtures were recorded from, and ACP's public schema is pinned by an
    ``API-Version`` header rather than by a versioned artifact we can vendor.

    So the *sentence* is read out of the boundary too, and not only the boundary label.
    Reporting ``COMPATIBLE_INTERFACE`` in one field while asserting "validated locally
    against the pinned public schema" in the next would be a status object that contradicts
    itself, and the half a reader quotes is always the sentence. There is no schema bundle
    in this repository for ACP; :data:`CLAIM_FOR_BOUNDARY` is where that fact becomes the
    words a status endpoint prints.
    """
    resolved = pin if pin is not None else PINS[Protocol.ACP]
    return SurfaceStatus(
        protocol=resolved.protocol,
        version=resolved.version,
        source_ref=resolved.source_ref,
        boundary=resolved.boundary,
        claim=assert_claim_permitted(CLAIM_FOR_BOUNDARY[resolved.boundary]),
        disclaimer=resolved.disclaimer,
    )
