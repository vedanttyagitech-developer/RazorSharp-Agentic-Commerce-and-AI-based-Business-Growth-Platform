"""Shared kernel contracts.

Fixed before the implementation modules so that each can be written and tested against a
stable surface. Nothing here performs I/O; these are the value objects that cross module
boundaries inside the kernel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from commerce_domain import Money

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


class Operation(StrEnum):
    """Provider mutations. Each consumes exactly one Execution Grant."""

    PAYMENT_CREATE_ORDER = "PAYMENT_CREATE_ORDER"
    REFUND_EXECUTE = "REFUND_EXECUTE"
    RESERVE_DEBIT = "RESERVE_DEBIT"


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
class VerifiedAuthorityProof:
    """What a protocol gateway hands the kernel after verification, specification 13.1.

    The kernel never parses a JWS, SD-JWT or raw protocol payload. It receives this and
    independently re-checks every field against locked rows.
    """

    protocol: str
    protocol_version: str
    issuer: str
    subject: str
    key_id: str
    algorithm: str
    mandate_ref: str
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout: CheckoutRef
    amount: Money
    action: str
    expires_at: datetime
    authority_epoch: int
    verification_receipt_id: str
    correlation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Delta:
    """One material difference between an approved checkout and current merchant state."""

    field_path: str
    approved: Any
    current: Any
    reason: str


@dataclass(frozen=True, slots=True)
class KernelDecision:
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
