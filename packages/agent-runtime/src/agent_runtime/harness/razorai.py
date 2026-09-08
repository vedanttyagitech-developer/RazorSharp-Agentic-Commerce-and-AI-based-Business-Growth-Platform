"""The RazorAI: the buyer-side harness of specification 6.1, with no model in it.

It serves a buyer principal, routes among Shopping, Checkout and Support, and refuses a
merchant principal outright. A merchant's principal carries merchant scope and no buyer
scope; letting it through would let a merchant session build a cart in a buyer's name
and the audit trail would then attribute a purchase to a buyer who never asked for one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from commerce_domain import ActorType, AgentPrincipal

from ..language import Language
from .base import Harness, is_merchant_principal
from .routing import BUYER_SPECIALISTS, Clarification, Route, route_buyer
from .session import CopilotSession

__all__ = ["RazorAI"]


class RazorAI(Harness):
    """Routes a buyer's turn to Shopping, Checkout or Support. Owns the buyer session."""

    specialists = BUYER_SPECIALISTS

    #: The actors this harness is for. An allowlist, so an actor added later is refused
    #: rather than routed to a buyer's copilot by default -- which is how MERCHANT would
    #: have arrived: not in the old refused tuple, and caught only by the merchant-scope
    #: check below it, which depended on a session having no buyer reference.
    ACCEPTS: ClassVar[frozenset[ActorType]] = frozenset(
        {ActorType.BUYER, ActorType.AGENT, ActorType.PROTOCOL}
    )

    def accepts(self, principal: AgentPrincipal) -> bool:
        if principal.actor_type not in self.ACCEPTS:
            return False
        return not is_merchant_principal(principal)

    def route(
        self,
        text: str,
        language: Language,
        context: Mapping[str, Any],
        session: CopilotSession,
    ) -> Route | Clarification:
        return route_buyer(text, language, context, session)
