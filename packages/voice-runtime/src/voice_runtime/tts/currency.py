"""Render verified INR amounts without choosing a speech provider."""

from __future__ import annotations

import re
from decimal import Decimal

from commerce_domain import Money

from .templates import Locale, money_to_words

# Rendering happens after grounding. Only explicitly INR-marked amounts are rewritten;
# foreign prices and bare numbers are never silently relabelled.
_INR_AMOUNT = re.compile(
    r"(?:₹\s*|\bINR\s+|\bRs\.?\s*)([0-9][0-9,]*(?:\.[0-9]{1,2})?)(?![0-9]|\.[0-9])"
    r"|(?<![0-9.])([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:INR|rupees?|रुपये|रुपए)(?!\w)",
    re.IGNORECASE,
)


def spoken_currency(text: str, locale: Locale) -> str:
    def render(match: re.Match[str]) -> str:
        amount = Decimal((match.group(1) or match.group(2)).replace(",", ""))
        return money_to_words(Money(minor=int(amount * 100), currency="INR"), locale)

    return _INR_AMOUNT.sub(render, text)
