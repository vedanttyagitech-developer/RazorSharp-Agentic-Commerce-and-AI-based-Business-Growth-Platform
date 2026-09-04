"""The Merchant Copilot: the merchant-side harness of specification 6.5, with no model.

It serves a principal minted from the merchant's authenticated server session (tenant and
merchant scope, no buyer scope) and routes between Growth and Case. A buyer principal is
refused before anything runs: the merchant specialists read tenant-wide metrics and the
human-review queue, and a buyer's principal must never be the one those reads are
attributed to. The refusal is an exception, not a reply, because it is an authorization
boundary and the API answers it with a status code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from transaction_kernel import AgentPrincipal

from ..language import Language
from .base import Harness, is_merchant_principal
from .routing import MERCHANT_SPECIALISTS, Clarification, Route, route_merchant
from .session import CopilotSession

__all__ = ["MerchantCopilot"]


class MerchantCopilot(Harness):
    """Routes a merchant's turn to Growth or Case. Refuses every buyer principal."""

    specialists = MERCHANT_SPECIALISTS

    def accepts(self, principal: AgentPrincipal) -> bool:
        return is_merchant_principal(principal)

    def route(
        self,
        text: str,
        language: Language,
        context: Mapping[str, Any],
        session: CopilotSession,
    ) -> Route | Clarification:
        return route_merchant(text, language, context, session)
