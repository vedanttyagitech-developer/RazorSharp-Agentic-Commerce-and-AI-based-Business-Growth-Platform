"""A deliberately narrow read-only discovery grammar; other requests keep reasoning.

Only complete category requests match. Budgets, comparisons, attributes, cart changes,
pronouns and compound requests cannot lose their meaning in a keyword shortcut.
"""

from __future__ import annotations

import re
import unicodedata

_CATEGORIES = {
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
    r"(?:now\s+)?(?:show(?:\s+me)?|find(?:\s+me)?|search(?:\s+for)?)\s+(.+?)(?:\s+(?:products|options))?",
    r"(?:ab\s+)?(?:mujhe\s+)?(.+?)(?:\s+(?:products|options))?\s+(?:dikhao|dikhaiye)",
    r"(?:अब\s+)?(?:मुझे\s+)?(.+?)(?:\s+(?:ऑप्शंस|ऑप्शन्स|विकल्प|प्रोडक्ट्स))?\s+(?:दिखाओ|दिखाइए|दिखाइये)",
)


def discovery_query(message: str) -> str | None:
    value = unicodedata.normalize("NFKC", message).casefold().strip(" .!?।")
    value = re.sub(r"^(?:please|प्लीज)\s+|\s+(?:please|प्लीज)$", "", value)
    if value in _CATEGORIES:
        return _CATEGORIES[value]
    for pattern in _PATTERNS:
        match = re.fullmatch(pattern, value)
        if match and match[1] in _CATEGORIES:
            return _CATEGORIES[match[1]]
    return None


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
