"""The one internal shape every protocol request is translated into.

Step 4 of specification 13.1: "Maps protocol objects into typed internal commands." This is
that typed internal command, and the discipline it enforces is the whole reason
specification 13.1 calls the core protocol-neutral.

Without it, four adapters would each hand the services below them a slightly different
thing, and "the same kernel invariant across trusted UI, UCP/AP2, ACP and MCP entry points"
(29.5) would be four separate implementations that happen to agree today. With it, an
adapter's entire job is to produce one of these, and everything after the translation is
shared code that cannot tell which protocol it came from.

The vocabulary is deliberately smaller than any single protocol's. UCP has a rich
post-purchase surface, ACP has its own session lifecycle, MCP has tools; all of them
collapse onto the same short list of things a buyer can actually want, because that list is
what the platform beneath knows how to do. An adapter that cannot express a request as one
of these is an adapter being asked for something this platform does not offer, and the
right answer is a refusal rather than a widened enum.

Two things an intent deliberately cannot express, and both absences are load-bearing:

**Consent.** There is no ``APPROVE`` and no ``REJECT``. A protocol caller cannot form the
request, so no amount of downstream bugs can honour one. Approval happens on the trusted
buyer surface, against a hash the buyer was shown (specification 5.3, invariant 15).

**Execution.** There is no ``PAY``, ``REFUND`` or ``CANCEL`` -- only ``PROPOSE_*``. A
proposal is a request for a human decision, and naming it that way in the type system is
what stops "the model asked to refund" from ever being one rename away from "the model
refunded".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from commerce_domain import Money

from .identity import AuthenticatedCaller
from .pins import ProtocolPin

__all__ = ["IntentKind", "ProtocolIntent"]


class IntentKind(StrEnum):
    """Everything an external party may ask this platform for.

    Closed on purpose, and shorter than any protocol's own surface. Adding a member is a
    decision about what external parties may do, which is exactly the kind of decision that
    should require an edit here and a review, rather than falling out of an adapter.
    """

    #: Grounded catalogue search or product lookup. Reads only.
    DISCOVER = "DISCOVER"
    #: Availability for a specific item, at a moment, from authoritative merchant state.
    CHECK_INVENTORY = "CHECK_INVENTORY"
    #: Create or amend a basket. No money, no authority, fully reversible.
    BUILD_BASKET = "BUILD_BASKET"
    #: Turn a basket into an immutable, hashed, reserved checkout version awaiting consent.
    CREATE_CHECKOUT = "CREATE_CHECKOUT"
    #: Ask for the buyer's decision. Produces an approval request, never an approval.
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    #: Submit a version a human has already approved to the same kernel admission every
    #: other surface uses. The strongest thing a protocol caller can do, and it still
    #: cannot move money by itself -- admission decides that.
    SUBMIT_APPROVED = "SUBMIT_APPROVED"
    #: Read an order's state and its capture evidence.
    TRACK_ORDER = "TRACK_ORDER"
    #: Ask a human to consider a cancellation. Names no amount (specification 29.4).
    PROPOSE_CANCELLATION = "PROPOSE_CANCELLATION"
    #: Ask a human to consider a refund. Names no amount, for the same reason.
    PROPOSE_REFUND = "PROPOSE_REFUND"
    #: Hand the conversation to a person, creating at most one support case.
    ESCALATE_SUPPORT = "ESCALATE_SUPPORT"


#: Intents that read and change nothing. Used by adapters to decide whether a request needs
#: an idempotency key, a replay guard or a mutation transaction at all.
READ_ONLY_INTENTS: frozenset[IntentKind] = frozenset(
    {IntentKind.DISCOVER, IntentKind.CHECK_INVENTORY, IntentKind.TRACK_ORDER}
)

#: Intents that ask a human to decide something. They create a request, never an outcome.
PROPOSAL_INTENTS: frozenset[IntentKind] = frozenset(
    {
        IntentKind.REQUEST_APPROVAL,
        IntentKind.PROPOSE_CANCELLATION,
        IntentKind.PROPOSE_REFUND,
        IntentKind.ESCALATE_SUPPORT,
    }
)


@dataclass(frozen=True, slots=True)
class ProtocolIntent:
    """One external request, normalised, before anything has been decided about it.

    ``amount`` is :class:`~commerce_domain.Money` -- integer minor units -- because that is
    the only representation of money that exists anywhere in this platform. Protocols carry
    amounts in several shapes and one of an adapter's jobs is to land them here without
    ever going through a float.

    ``arguments`` is the protocol-shaped remainder: a SKU, a quantity, a search phrase. It
    is a mapping rather than a union of typed payloads because the alternative is a type
    per protocol per intent, and the services below validate their own inputs anyway. It
    carries no authority: nothing in it may widen a capability, name a tenant or set an
    amount, and the adapters are written so it cannot.

    ``raw_reference`` points at the evidence record holding the original request, so a
    reviewer reading an intent can always get back to the bytes that produced it (13.3).
    """

    kind: IntentKind
    caller: AuthenticatedCaller
    pin: ProtocolPin
    correlation_id: uuid.UUID
    #: The protocol's own identifier for this interaction, echoed into evidence.
    external_id: str | None = None
    checkout_id: uuid.UUID | None = None
    checkout_version: int | None = None
    content_hash: str | None = None
    order_id: uuid.UUID | None = None
    amount: Money | None = None
    arguments: Any = field(default_factory=lambda: MappingProxyType({}))
    raw_reference: str | None = None

    @property
    def is_read_only(self) -> bool:
        return self.kind in READ_ONLY_INTENTS

    @property
    def is_proposal(self) -> bool:
        """True when honouring this intent produces a request for a human, not an outcome."""
        return self.kind in PROPOSAL_INTENTS

    @property
    def moves_money(self) -> bool:
        """True only for the one intent that can reach kernel admission.

        Everything else is discovery, construction or a proposal. Written as a property
        rather than left implicit so that a future intent has to answer this question
        explicitly at the point it is added.
        """
        return self.kind is IntentKind.SUBMIT_APPROVED
