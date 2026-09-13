"""A deliberately narrow read-only discovery grammar; other requests keep reasoning.

Product names, brands and categories are not restricted to a product allowlist.
Budgets, comparisons, attributes, cart changes,
pronouns and compound requests cannot lose their meaning in a keyword shortcut.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from agent_runtime.shopping_intent import NamedCartIntent

_CATEGORIES = {
    "iphone": "iphone",
    "iphones": "iphone",
    "आईफोन": "iphone",
    "आईफ़ोन": "iphone",
    "milk": "milk",
    "doodh": "milk",
    "dudh": "milk",
    "दूध": "milk",
    "bread": "bread",
    "ब्रेड": "bread",
    "eggs": "eggs",
    "अंडे": "eggs",
    "curd": "curd",
    "dahi": "curd",
    "दही": "curd",
    "butter": "butter",
    "cheese": "cheese",
    "paneer": "paneer",
    "पनीर": "paneer",
    "chips": "chips",
    "biscuits": "biscuits",
    "chocolate": "chocolate",
    "tea": "tea",
    "coffee": "coffee",
    "rice": "rice",
    "atta": "atta",
    "shampoo": "shampoo",
    "soap": "soap",
    "toothpaste": "toothpaste",
    "earphones": "earphones",
    "chargers": "chargers",
    "batteries": "batteries",
}
_PATTERNS = (
    r"(?:can\s+you\s+)?(?:now\s+)?(?:show(?:\s+me)?|find(?:\s+me)?|search(?:\s+for)?)\s+(?:an?\s+)?(.+?)(?:\s+(?:products|options))?",
    r"(?:ab\s+)?(?:mujhe\s+)?(.+?)(?:\s+(?:products|options))?\s+(?:dikhao|dikhaiye)",
    r"(?:अब\s+)?(?:मुझे\s+)?(.+?)(?:\s+(?:ऑप्शंस|ऑप्शन्स|विकल्प|प्रोडक्ट्स))?\s+(?:दिखाओ|दिखाइए|दिखाइये)",
    r"(?:i(?:'m| am)\s+looking\s+for|i\s+(?:want|need))\s+(?:an?\s+)?(.+)",
)
_REASONING = re.compile(
    r"\b(bundle|picnic|party|meal|nashta|nashte|naashta|tulna|badlo|ki jagah|ke badle)\b|"
    r"तुलना|नाश्त|की जगह|के बदले|बदलो|"
    r"\b(under|below|above|within|budget|cheaper|cheapest|best|better|compare|versus|vs|"
    r"healthier|healthy|free|without|for|and|then|not|never|dont|don't|nahi|nahin|mat|"
    r"add|remove|replace|instead|swap|change|with|buy|pay|checkout|refund|cancel|order|track|reserve|those|these|it|"
    r"which|what|why|how|who|when|can|could|should|would|hello|hi|thanks|yes|no)\b"
    r"|नहीं|नही|मत\s|सस्ता|सस्ते|बजट|तुलना|के लिए|से कम|से ज्यादा|और\s|भुगतान|जोड़|हटाओ"
)


def _literal_product_query(value: str) -> str | None:
    """Preserve the whole catalogue query; never pick one keyword out of a request."""
    if not value or len(value) > 100 or len(value.split()) > 10 or _REASONING.search(value):
        return None
    if any(
        not (
            character.isspace()
            or unicodedata.category(character)[0] in "LNM"
            or character in "-+&.'’"
        )
        for character in value
    ):
        return None
    return _CATEGORIES.get(value, value)


def discovery_query(message: str) -> str | None:
    value = unicodedata.normalize("NFKC", message).casefold().strip(" .!?।")
    value = re.sub(r"^(?:please|प्लीज)\s+|\s+(?:please|प्लीज)$", "", value)
    if value in _CATEGORIES:
        return _CATEGORIES[value]
    for pattern in _PATTERNS:
        match = re.fullmatch(pattern, value)
        if match:
            return _literal_product_query(match[1])
    # Bare names work too, but ordinary conversation must not become a catalogue query.
    if re.search(r"\b(i|you|we|me|my|is|are|do|does|show|find|search|want|need)\b", value):
        return None
    return _literal_product_query(value)


def discovery_queries(message: str) -> tuple[str, ...] | None:
    """A bounded list of literal searches, never a compound cart or budget request."""
    value = unicodedata.normalize("NFKC", message).casefold().strip(" .!?।")
    value = re.sub(r"^(?:please|प्लीज)\s+|\s+(?:please|प्लीज)$", "", value)
    for pattern in _PATTERNS:
        match = re.fullmatch(pattern, value)
        if match:
            value = match[1]
            break
    parts = re.split(r"\s+(?:and|aur|और)\s+|\s*,\s*", value)
    if not 2 <= len(parts) <= 4:
        return None
    queries = [discovery_query(part) for part in parts]
    if any(query is None for query in queries):
        return None
    return tuple(dict.fromkeys(query for query in queries if query))


def discovery_reply(language: str, found: bool) -> str:
    if language == "hi":
        return (
            "ये विकल्प मिले हैं। आप कौन सा लेना चाहेंगे?" if found else "अभी कोई विकल्प नहीं मिला। कुछ और खोजें?"
        )
    if language == "hi-Latn":
        return (
            "Ye options mile hain. Kaunsa lena chahenge?"
            if found
            else "Abhi koi option nahi mila. Kuch aur dhoondhein?"
        )
    return (
        "Here are the options. Which would you like?"
        if found
        else "No matches right now. Shall we try something else?"
    )


def prefer_named_hits(
    query: str, hits: list[dict[str, Any]], *, strict: bool = False
) -> list[dict[str, Any]]:
    """Prefer direct product-name matches over accessories matched only by search aliases."""
    from .search_constraints import _terms

    tokens = _terms(query)
    direct = [
        hit
        for hit in hits
        if tokens.issubset(
            _terms(
                " ".join(str(hit.get(key, "")) for key in ("display_name", "name_en", "name_hi"))
            )
        )
    ]
    return direct if strict else direct or hits


def single_product_add(message: str) -> bool:
    """Only an explicit one-item add; questions, quantities and compound requests reason."""
    value = unicodedata.normalize("NFKC", message).casefold().strip(" .!।")
    return bool(
        re.fullmatch(
            r"(?:please\s+)?add\s+(?:it|this|that)(?:\s+to\s+(?:my\s+|the\s+)?cart)?(?:\s+please)?"
            r"|(?:isko|ise|ye|yeh)\s+(?:cart\s+(?:mein|me)\s+)?(?:add|daal|dal)\s+(?:karo|do)"
            r"|(?:इसे|इसको|यह)\s+(?:कार्ट\s+में\s+)?(?:जोड़ो|जोड़ दो|डालो|डाल दो|ऐड करो)",
            value,
        )
    )


def named_product_add(message: str) -> str | None:
    """An explicit one-product add; quantities, negation and constraints need reasoning."""
    if single_product_add(message):
        return None
    value = unicodedata.normalize("NFKC", message).casefold().strip(" .!।")
    for pattern in (
        r"(?:please\s+)?add\s+(.+?)(?:\s+to\s+(?:my\s+|the\s+)?cart)?",
        r"(.+?)\s+(?:cart\s+(?:mein|me)\s+)?add\s+karo",
        r"(.+?)\s+कार्ट\s+में\s+जोड़\s+दो",
    ):
        match = re.fullmatch(pattern, value)
        if match:
            query = match[1]
            if re.search(
                r"^(?:\d+|one|two|three|four|a|an|ek|do|एक|दो)\s|\b(it|this|that|isko|ise|ye|yeh)\b",
                query,
            ):
                return None
            return _literal_product_query(query)
    return None


def named_quantity_add(message: str) -> tuple[str, int] | None:
    """Resolve multilingual explicit counts without dropping product constraints."""
    intent = resolved_named_intent(message)
    return (intent.query, intent.quantity) if intent and intent.mode == "add" else None


def resolved_named_intent(message: str) -> NamedCartIntent | None:
    from dataclasses import replace

    from agent_runtime.shopping_intent import named_cart_intent

    intent = named_cart_intent(message)
    if intent and re.search(r"(?:^|\s)(?:or|ya|मत|या)(?:\s|$)", intent.query):
        return None
    query = _literal_product_query(intent.query) if intent else None
    return replace(intent, query=query) if intent is not None and query is not None else None
