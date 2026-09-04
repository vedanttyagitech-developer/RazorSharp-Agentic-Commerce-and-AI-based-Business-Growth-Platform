"""Deterministic routing: which specialist answers this turn, and why.

The reference runtime lets a model route (``commerce_common/delegation.py``). We do not,
because routing is a hop on the money path and a model hop is the one step that cannot be
unit-tested for certainty. Routing here is a function of three inputs, tried in order:

1. **Explicit context** -- what the buyer is looking at. A ``checkout_id`` in the request
   context routes to Checkout; an ``order_id`` to Support; a ``page`` name to whichever
   specialist owns that page. These come from the authenticated request, never the body
   of the message.
2. **Cheap intent signals** over the utterance: whole-word lexicons in English, Hindi and
   Hinglish, tried in precedence order (support before checkout before shopping, because
   "cancel my order" contains "order" and must not become a purchase).
3. **Session continuity** -- a follow-up with no signal goes to the specialist that
   answered last turn.

When none fires the result is a :class:`Clarification`: a deterministic question in the
buyer's language. The harness renders it and runs no specialist, so an unroutable
message costs no model call and changes no state.

The honest cost of a lexicon is that it misroutes more often than a model would. A
misroute costs one typed hand-back (:mod:`agent_runtime.harness.base`) and changes
nothing, which is why it is acceptable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from ..capabilities.registry import AgentRole
from ..language import Language
from .session import CopilotSession

__all__ = [
    "BUYER_SPECIALISTS",
    "MERCHANT_SPECIALISTS",
    "Clarification",
    "Route",
    "Specialist",
    "route_buyer",
    "route_merchant",
]


#: The five model-backed specialists of the roster. Nothing else is an agent. This is the
#: registry's ``AgentRole`` under the harness's name, not a second enumeration of the same
#: five strings: the router, the binding, the tool factory and the specialist specs must
#: all mean the same object when they say "shopping", or a drift between two enums would
#: have to be caught by a test instead of being impossible.
Specialist = AgentRole


BUYER_SPECIALISTS: Final[frozenset[Specialist]] = frozenset(
    {Specialist.SHOPPING, Specialist.CHECKOUT, Specialist.SUPPORT}
)
MERCHANT_SPECIALISTS: Final[frozenset[Specialist]] = frozenset({Specialist.GROWTH, Specialist.CASE})


@dataclass(frozen=True, slots=True)
class Route:
    """The specialist chosen and the reason, in a form a test can assert on exactly."""

    specialist: Specialist
    reason: str


@dataclass(frozen=True, slots=True)
class Clarification:
    """No specialist fits. ``question`` is template text; ``reason`` says which tier failed."""

    question: str
    reason: str


# ------------------------------------------------------------------------ lexicons


def _pattern(terms: Iterable[str]) -> re.Pattern[str]:
    """Whole-word alternation. ``\\w`` is Unicode-aware, so Devanagari words bound correctly."""
    alternatives = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


# Support: post-purchase and remedy. Tried first so "cancel my order" never routes to a
# purchase specialist because it contains "order".
_SUPPORT: Final[re.Pattern[str]] = _pattern(
    {
        "refund", "refunds", "refunded", "cancel", "cancellation", "complaint", "complain",
        "damaged", "missing", "not delivered", "never arrived", "wrong item", "late",
        "where is my order", "track my order", "order status", "escalate", "support",
        "return", "returns", "help with my order", "chargeback", "money back",
        # Hinglish
        "wapas", "vapas", "wapsi", "shikayat", "kharab", "nahi aaya", "nahin aaya",
        "order kahan", "order kaha", "cancel karo", "cancel kardo", "paisa wapas",
        # Hindi
        "रिफंड", "वापसी", "वापस", "शिकायत", "रद्द", "खराब", "नहीं आया", "ऑर्डर कहाँ", "ऑर्डर कहां",
    }
)  # fmt: skip

# Checkout: paying for what is already in the basket.
_CHECKOUT: Final[re.Pattern[str]] = _pattern(
    {
        "checkout", "check out", "pay", "payment", "approve", "approval", "place order",
        "place the order", "buy now", "proceed", "confirm order", "total", "bill",
        "submit", "upi", "card",
        # Hinglish
        "bhugtan", "payment karo", "order karo", "order kar do", "order place", "kitna total",
        "bill kitna", "checkout karo",
        # Hindi
        "भुगतान", "चेकआउट", "ऑर्डर करो", "ऑर्डर कर दो", "कुल", "बिल",
    }
)  # fmt: skip

# Shopping: finding and basket building. Deliberately generic verbs and quantity words,
# never product names, so a new catalogue needs no new lexicon.
_SHOPPING: Final[re.Pattern[str]] = _pattern(
    {
        "search", "find", "show", "looking for", "want", "need", "add", "remove", "basket",
        "cart", "price", "cost", "how much", "cheap", "cheapest", "compare", "recommend",
        "suggest", "buy", "get me", "do you have", "available", "in stock",
        "quantity", "more", "less",
        # Hinglish
        "chahiye", "chahie", "chaiye", "dikhao", "dikhaiye", "batao", "bataiye", "dedo",
        "daalo", "dalo", "hatao", "nikalo", "kharido", "kharidna", "kitna", "kitne", "kitni",
        "sasta", "sasti", "milega", "milegi", "hai kya", "add karo", "lao", "laao",
        # Hindi
        "चाहिए", "दिखाओ", "दिखाइए", "बताओ", "बताइए", "खरीदना", "कितना", "कितने", "कितनी",
        "सस्ता", "सस्ती", "मिलेगा", "मिलेगी", "जोड़ो", "हटाओ", "ढूंढो", "खोजो",
    }
)  # fmt: skip

# Merchant side. Case: the human-review queue and its evidence.
_CASE: Final[re.Pattern[str]] = _pattern(
    {
        "case", "cases", "escalation", "escalations", "ticket", "tickets", "dispute",
        "disputes", "review queue", "human review", "chargeback", "complaint", "complaints",
        "stuck payment", "stuck refund", "reconciliation",
        "मामला", "शिकायत", "विवाद",
    }
)  # fmt: skip

# Growth: metrics and proposals over deterministic data.
_GROWTH: Final[re.Pattern[str]] = _pattern(
    {
        "sales", "revenue", "conversion", "abandonment", "abandoned", "metrics", "metric",
        "discount", "discounts", "campaign", "campaigns", "proposal", "proposals",
        "catalogue health", "catalog health", "inventory", "anomaly", "anomalies", "stock",
        "pricing", "growth", "performance", "trend", "trends", "checkout rate", "orders today",
        "बिक्री", "राजस्व", "छूट", "स्टॉक", "प्रस्ताव",
    }
)  # fmt: skip


# ------------------------------------------------------------------ explicit context

_BUYER_PAGES: Final[Mapping[str, Specialist]] = {
    "checkout": Specialist.CHECKOUT,
    "approval": Specialist.CHECKOUT,
    "payment": Specialist.CHECKOUT,
    "order": Specialist.SUPPORT,
    "orders": Specialist.SUPPORT,
    "support": Specialist.SUPPORT,
    "refund": Specialist.SUPPORT,
    "basket": Specialist.SHOPPING,
    "cart": Specialist.SHOPPING,
    "catalogue": Specialist.SHOPPING,
    "catalog": Specialist.SHOPPING,
    "search": Specialist.SHOPPING,
    "product": Specialist.SHOPPING,
    "home": Specialist.SHOPPING,
}

_MERCHANT_PAGES: Final[Mapping[str, Specialist]] = {
    "cases": Specialist.CASE,
    "case": Specialist.CASE,
    "queue": Specialist.CASE,
    "review": Specialist.CASE,
    "growth": Specialist.GROWTH,
    "metrics": Specialist.GROWTH,
    "analytics": Specialist.GROWTH,
    "catalogue": Specialist.GROWTH,
    "inventory": Specialist.GROWTH,
    "proposals": Specialist.GROWTH,
}


def _context_str(context: Mapping[str, Any], key: str) -> str | None:
    value = context.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ------------------------------------------------------------------- clarifications

_CLARIFY_BUYER: Final[Mapping[Language, str]] = {
    Language.EN: (
        "I can help you find products, pay for your basket, or sort out an existing order. "
        "Which of those do you need?"
    ),
    Language.HI: (
        "मैं सामान ढूँढने, बास्केट का भुगतान करने, या किसी पुराने ऑर्डर में मदद कर सकता हूँ। आपको इनमें से क्या चाहिए?"
    ),
    Language.HI_LATN: (
        "Main saman dhoondhne, basket ka payment karne, ya kisi purane order mein madad kar "
        "sakta hoon. Aapko inme se kya chahiye?"
    ),
}

_CLARIFY_MERCHANT: Final[Mapping[Language, str]] = {
    Language.EN: (
        "I can look at your store's performance and draft growth proposals, or walk you "
        "through the cases in your review queue. Which would you like?"
    ),
    Language.HI: (
        "मैं आपकी दुकान का प्रदर्शन देखकर ग्रोथ प्रस्ताव बना सकता हूँ, या समीक्षा कतार के "
        "मामले समझा सकता हूँ। आप क्या चाहेंगे?"
    ),
    Language.HI_LATN: (
        "Main aapki dukaan ka performance dekh kar growth proposals bana sakta hoon, ya review "
        "queue ke cases samjha sakta hoon. Aap kya chahenge?"
    ),
}


# ------------------------------------------------------------------------- routing


def route_buyer(
    text: str,
    language: Language,
    context: Mapping[str, Any],
    session: CopilotSession,
) -> Route | Clarification:
    """Route one buyer message. Explicit context, then intent, then continuity."""
    if _context_str(context, "checkout_id"):
        return Route(Specialist.CHECKOUT, "context:checkout_id")
    if _context_str(context, "order_id"):
        return Route(Specialist.SUPPORT, "context:order_id")
    page = _context_str(context, "page")
    if page and (target := _BUYER_PAGES.get(page.casefold())) is not None:
        return Route(target, f"context:page={page.casefold()}")

    if _SUPPORT.search(text):
        return Route(Specialist.SUPPORT, "intent:support")
    if _CHECKOUT.search(text):
        return Route(Specialist.CHECKOUT, "intent:checkout")
    if _SHOPPING.search(text):
        return Route(Specialist.SHOPPING, "intent:shopping")

    # A message that names a catalogue SKU is a shopping question even without a verb.
    if re.search(r"\b[A-Z]{2,6}-[A-Z]{2,8}-\d{2,4}\b", text):
        return Route(Specialist.SHOPPING, "intent:sku_token")

    if session.last_specialist in BUYER_SPECIALISTS:
        return Route(Specialist(session.last_specialist), "session:continuity")
    if session.checkout_id:
        return Route(Specialist.CHECKOUT, "session:checkout_open")
    if session.basket_id:
        return Route(Specialist.SHOPPING, "session:basket_open")

    return Clarification(_CLARIFY_BUYER[language], "unroutable")


def route_merchant(
    text: str,
    language: Language,
    context: Mapping[str, Any],
    session: CopilotSession,
) -> Route | Clarification:
    """Route one merchant message. Same three tiers; the case queue outranks growth."""
    if _context_str(context, "case_id"):
        return Route(Specialist.CASE, "context:case_id")
    page = _context_str(context, "page")
    if page and (target := _MERCHANT_PAGES.get(page.casefold())) is not None:
        return Route(target, f"context:page={page.casefold()}")

    if _CASE.search(text):
        return Route(Specialist.CASE, "intent:case")
    if _GROWTH.search(text):
        return Route(Specialist.GROWTH, "intent:growth")

    if session.last_specialist in MERCHANT_SPECIALISTS:
        return Route(Specialist(session.last_specialist), "session:continuity")

    return Clarification(_CLARIFY_MERCHANT[language], "unroutable")
