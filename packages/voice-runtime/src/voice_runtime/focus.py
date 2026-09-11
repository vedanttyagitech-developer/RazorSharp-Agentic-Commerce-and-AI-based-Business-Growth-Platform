"""Conversation context fences shopping while a checkout is still loading.

This is routing context, not authorization. Checkout facts still require the
existing authenticated card reader; a failed read never reopens shopping tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


def requests_checkout_review(text: str) -> bool:
    """Recognise navigation only, matching the buyer's checkout-choice.ts guard.

    Fence the shopping reply before the client receives the transcript and opens
    review. This does not create a checkout, approve a bill, or initiate payment.
    """
    text = re.sub(r"[.!।]+$", "", text.strip().lower())
    text = re.sub(r"\bcheck\s+out\b", "checkout", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^(okay|ok|yes|yeah|haan|han|हाँ)[, ]+", "", text)
    text = re.sub(r"^let['’]s ", "", text)
    text = re.sub(r"^(ab|chalo|चलो|अब)\s+", "", text)
    if re.search(
        r"\b(no|not|never|cancel|nahi|nahin|mat|dont|what|how|why)\b"
        r"|don['’]?t|नहीं|नही|मत|क्या|कैसे|क्यों|\?",
        text,
    ):
        return False
    return bool(
        re.fullmatch(
            r"(please\s+)?(checkout|go to checkout|take me to checkout"
            r"|take me to review screen|proceed to checkout"
            r"|proceed (to|for) payment|proceed to pay|go to payment"
            r"|take me to payment|continue to payment"
            r"|review (my |the )?(order|bill|cart)|show (me )?(my |the )?(bill|checkout)"
            r"|place (my |the )?order)(\s+please)?"
            r"|(mera |meri )?(bill|order) (dikhao|dikha do|review karo)"
            r"|checkout (karo|kar do|dikhao)"
            r"|(checkout|payment|पेमेंट|भुगतान|चेकआउट)\s+(pe|par|पे|पर)\s+"
            r"(chal|chalo|chale|chalein|chalen|chaliye|chalte hain|le chalo|चल|चलो|चलें|चलिए)"
            r"|(review|रिव्यू)( (karo|kar do|करो))?"
            r"|(मेरा |मेरी )?(बिल|ऑर्डर) (दिखाओ|दिखाइए|दिखा दो)|चेकआउट (करो|दिखाओ)",
            text,
        )
    )


@dataclass(slots=True)
class ConversationFocus:
    scope: Literal["shopping", "checkout_pending", "checkout_ready", "checkout_unavailable"] = (
        "shopping"
    )
    revision: int = 0

    @property
    def shopping_allowed(self) -> bool:
        return self.scope == "shopping"

    def enter_checkout(self) -> int:
        self.revision += 1
        self.scope = "checkout_pending"
        return self.revision

    def resolve(self, revision: int, *, verified: bool) -> bool:
        if self.scope == "shopping" or revision != self.revision:
            return False
        self.scope = "checkout_ready" if verified else "checkout_unavailable"
        return True

    def leave_checkout(self) -> None:
        self.revision += 1
        self.scope = "shopping"


def requests_shop_navigation(text: str) -> bool:
    """UI-only commands must not become catalogue searches or financial mutations."""
    text = re.sub(r"[.!?।]+$", "", text.lower().strip())
    text = re.sub(r"^(please|can you|could you)\s+", "", text)
    text = re.sub(r"\s+please$", "", text)
    return bool(
        re.fullmatch(
            r"(show|open)( me)?( my| the)? (basket|cart)"
            r"|(mera |meri )?(cart|basket) (dikhao|kholo)"
            r"|(मेरा |मेरी )?(कार्ट|बास्केट) (दिखाओ|खोलो)"
            r"|(cancel|close|exit)( the| my)? (checkout|order review|review|checkout review)"
            r"|(checkout|review|order review) (cancel|band)( karo| kar do)?"
            r"|(चेकआउट|रिव्यू) (बंद|कैंसल) करो",
            text,
        )
    )
