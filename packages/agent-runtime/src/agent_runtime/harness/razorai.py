"""The RazorAI: the buyer-side harness of specification 6.1, with no model in it.

It serves a buyer principal, routes among Shopping, Checkout and Support, and refuses a
merchant principal outright. A merchant's principal carries merchant scope and no buyer
scope; letting it through would let a merchant session build a cart in a buyer's name
and the audit trail would then attribute a purchase to a buyer who never asked for one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from transaction_kernel import ActorType, AgentPrincipal

from ..language import Language
from .base import Harness, is_merchant_principal
from .routing import BUYER_SPECIALISTS, Clarification, Route, route_buyer
from .session import CopilotSession

__all__ = ["RazorAI"]


class RazorAI(Harness):
    """Routes a buyer's turn to Shopping, Checkout or Support. Owns the buyer session."""

    specialists = BUYER_SPECIALISTS

    def accepts(self, principal: AgentPrincipal) -> bool:
        if principal.actor_type in (ActorType.OPERATOR, ActorType.WORKER, ActorType.SYSTEM):
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
