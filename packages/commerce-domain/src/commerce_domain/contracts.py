"""Neutral contracts: the vocabulary every plane shares and none of them owns.

These are the value objects that cross package boundaries -- who is asking, which checkout
version, what moved, and what the platform decided. Nothing here performs I/O, decides
anything, or holds authority. A type in this module can be read by a component that must
never be able to *act*, which is the whole reason the module exists.

Why they are not in the kernel
------------------------------
They were, and the arrangement failed a test it was never given. The buyer copilot and the
voice runtime need this vocabulary and nothing else from the kernel: they render an
``AdmissionDecision`` into a sentence, name the ``Delta`` fields that moved, and speak a
``RecoveryCode``. To do that they declared ``transaction-kernel`` as a dependency -- which
also put ``admit``, ``authorize``, ``append`` and every financial write one import line
away from a package a model drives. Nothing prevented that line being written. Nobody had
written it, which is a fact about the past rather than a property of the system, and this
project's claim is the second kind.

So the vocabulary moved down to the domain package, which those runtimes may depend on,
and the kernel dependency was removed from them outright. The kernel still uses all of
these -- it imports them from here, one direction, downwards.

What stayed behind, and why
---------------------------
``Operation`` and ``VerifiedAuthorityProof`` are still kernel types. Both name things only
the kernel may do: an ``Operation`` is a provider mutation that consumes exactly one
Execution Grant, and a ``VerifiedAuthorityProof`` is what entitles a caller to one. Moving
them would have made the boundary tidier and the claim weaker.

Nothing about any value changed in the move. ``str()`` on a ``StrEnum`` member returns the
member's value, never its module, so no canonical hash, no stored audit row and no wire
payload depends on where these definitions sit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from .recovery import RecoveryCode


class ActorType(StrEnum):
    """Who is asking. Recorded on every audit event and policy decision.

    OPERATOR exists so that a future Registry D reviewer is attributable as a merchant
    decision rather than a buyer one; see specification 5.3.
    """

    BUYER = "BUYER"
    AGENT = "AGENT"
    PROTOCOL = "PROTOCOL"
    OPERATOR = "OPERATOR"
    WORKER = "WORKER"
    SYSTEM = "SYSTEM"


class PolicyKind(StrEnum):
    """The families of merchant rule a sale is governed by, specification 10.2.1."""

    CANCELLATION = "CANCELLATION"
    REFUND = "REFUND"
    SUBSTITUTION = "SUBSTITUTION"
    DELIVERY = "DELIVERY"
    DISCOUNT = "DISCOUNT"
    FULFILMENT = "FULFILMENT"


#: Every kind must be present in every receipt, even when the merchant's answer is "no".
#: An omitted kind is the whole failure mode the receipt exists to prevent: at resolution
#: time a silent gap gets filled from the merchant's *current* policy, which is exactly
#: the retroactive change the receipt is supposed to make impossible. A merchant with no
#: substitution programme records ``{"allowed": False}`` and says so on the record.
REQUIRED_POLICY_KINDS: Final[frozenset[PolicyKind]] = frozenset(PolicyKind)


@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    """Immutable machine principal, specification 5.4.

    Constructed by the server at session start. A model cannot supply or edit one, and no
    product description, prompt or tool result can widen its capability set.
    """

    principal_id: str
    tenant_id: uuid.UUID
    actor_type: ActorType
    agent_role: str | None = None
    merchant_id: uuid.UUID | None = None
    buyer_ref: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    delegation_chain: tuple[str, ...] = ()
    correlation_id: uuid.UUID | None = None

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def subset_for(self, child_role: str, capabilities: frozenset[str]) -> AgentPrincipal:
        """Derive a sub-agent principal. A child can never exceed its parent."""
        if not capabilities <= self.capabilities:
            excess = sorted(capabilities - self.capabilities)
            raise ValueError(f"sub-agent would gain capabilities its parent lacks: {excess}")
        return AgentPrincipal(
            principal_id=f"{self.principal_id}/{child_role}",
            tenant_id=self.tenant_id,
            actor_type=self.actor_type,
            agent_role=child_role,
            merchant_id=self.merchant_id,
            buyer_ref=self.buyer_ref,
            capabilities=capabilities,
            delegation_chain=(*self.delegation_chain, self.principal_id),
            correlation_id=self.correlation_id,
        )


@dataclass(frozen=True, slots=True)
class CheckoutRef:
    """Identity of one immutable checkout version."""

    checkout_id: uuid.UUID
    version: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class Delta:
    """One material difference between an approved checkout and current merchant state."""

    field_path: str
    approved: Any
    current: Any
    reason: str


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The kernel's answer. Always structured, never prose.

    ``explanation`` is a stable reason key, not a sentence: the agent renders it into the
    buyer's language, and a template renders it into speech. Neither may alter the fields.
    """

    decision_id: uuid.UUID
    allowed: bool
    code: RecoveryCode
    explanation: str
    checkout: CheckoutRef | None = None
    deltas: tuple[Delta, ...] = ()
    grant_id: uuid.UUID | None = None
    payment_attempt_id: uuid.UUID | None = None
    next_version: int | None = None
    correlation_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.code is not RecoveryCode.OK:
            raise ValueError(f"allowed decision must carry OK, got {self.code}")
        if not self.allowed and self.code is RecoveryCode.OK:
            raise ValueError("denied decision cannot carry OK")
        if self.allowed and self.grant_id is None:
            raise ValueError(
                "an allowed decision must name the Execution Grant it issued; "
                "every provider mutation consumes exactly one"
            )


__all__ = [
    "REQUIRED_POLICY_KINDS",
    "ActorType",
    "AdmissionDecision",
    "AgentPrincipal",
    "CheckoutRef",
    "Delta",
    "PolicyKind",
]
