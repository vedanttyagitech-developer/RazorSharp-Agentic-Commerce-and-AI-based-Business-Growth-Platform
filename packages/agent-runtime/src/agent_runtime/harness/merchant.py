"""The Merchant Copilot: the merchant-side harness, with no model in it.

The mirror of :class:`~agent_runtime.harness.razorai.RazorAI`, and the two refuse each
other symmetrically. RazorAI refuses a merchant principal because letting one through
would let a merchant session build a cart in a buyer's name, and the audit trail would
then attribute a purchase to a buyer who never asked for one. This one refuses everything
that is not a merchant for the same reason read the other way: a buyer principal routed
here would reach a shop's confirmed sales and its change queue, which belong to whoever
owns the shop and to nobody else.

Neither harness is a model. This one serves a merchant principal and routes to Operations.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from commerce_domain import ActorType, AgentPrincipal

from ..language import Language
from .base import Harness, is_merchant_principal
from .routing import MERCHANT_SPECIALISTS, Clarification, Route, route_merchant
from .session import CopilotSession

__all__ = ["MerchantCopilot"]


class MerchantCopilot(Harness):
    """Routes a merchant's turn to Operations. Owns the merchant session."""

    specialists = MERCHANT_SPECIALISTS

    #: An allowlist, as RazorAI's is, so an actor type added later is refused rather than
    #: arriving here by default. Only MERCHANT: AGENT and PROTOCOL are on the buyer's list
    #: because a third party can shop on a buyer's behalf, and there is no equivalent story
    #: for the merchant surface -- nothing acts on a shop owner's behalf but the shop owner.
    ACCEPTS: ClassVar[frozenset[ActorType]] = frozenset({ActorType.MERCHANT})

    def accepts(self, principal: AgentPrincipal) -> bool:
        if principal.actor_type not in self.ACCEPTS:
            return False
        # Both halves, and the second is not redundant: the actor type says what a session
        # was minted as, and `is_merchant_principal` says whether it actually carries
        # merchant scope. A principal claiming to be a merchant while carrying a buyer
        # reference is the shape that would let a buyer read a shop, and it is refused
        # here rather than discovered at the first tool call.
        return is_merchant_principal(principal)

    def route(
        self,
        text: str,
        language: Language,
        context: Mapping[str, Any],
        session: CopilotSession,
    ) -> Route | Clarification:
        return route_merchant(text, language, context, session)
