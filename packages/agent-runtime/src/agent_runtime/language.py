"""Buyer-language detection.

The buyer's own text decides the language of a turn: the script the reply is written in
and the :class:`merchant_sim.Locale` the search tool asks for. The model is never asked
to choose either. A model can be talked into "the buyer prefers English" by a product
description; a deterministic classifier over the buyer's message cannot.

Detection is deliberately coarse. Devanagari anywhere means Hindi; otherwise a handful of
romanised Hindi function words mark Hinglish; otherwise English. Matching is multilingual
regardless -- merchant-sim folds Hindi, Hinglish and English onto one index -- so a
misclassification changes which script a product name is shown in, never which products
are found.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from merchant_sim import Locale
from merchant_sim.textfold import has_devanagari

__all__ = ["Language", "detect_language"]


class Language(StrEnum):
    """Buyer language for one turn. Chooses the reply script and the search display locale."""

    EN = "en"
    HI = "hi"
    HI_LATN = "hi-Latn"

    @property
    def locale(self) -> Locale:
        """The merchant-sim display locale this language maps to."""
        return _LOCALE_FOR[self]

    @property
    def label(self) -> str:
        """Human label used in the per-turn prompt footer."""
        return _LABEL_FOR[self]


_LOCALE_FOR: Final[dict[Language, Locale]] = {
    Language.EN: Locale.EN,
    Language.HI: Locale.HI,
    Language.HI_LATN: Locale.HI_LATN,
}

_LABEL_FOR: Final[dict[Language, str]] = {
    Language.EN: "English",
    Language.HI: "Hindi (Devanagari script)",
    Language.HI_LATN: "Hinglish (Hindi written in Latin script)",
}

# Romanised Hindi function words and verbs a buyer uses in a shopping request. None of
# these is an English word, so one hit is enough. "me", "do", "to", "the", "so" and other
# collisions are deliberately absent: a false Hinglish verdict would answer an English
# buyer in Hinglish, which is worse than the reverse.
_HINGLISH_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "mujhe", "muje", "mujhko", "hume", "humein", "hamein", "hamko",
        "chahiye", "chahiyen", "chaiye", "chahie", "chahta", "chahti", "chahte",
        "karo", "kardo", "karna", "karein", "kijiye", "kar",
        "dikhao", "dikhaiye", "batao", "bataiye", "bhejo", "bhejdo", "lao", "laao",
        "dedo", "dijiye", "daalo", "dalo", "hatao", "nikalo", "kharido", "kharidna",
        "kitna", "kitne", "kitni", "kaun", "kaunsa", "kaise", "kahan", "kab",
        "hai", "hain", "nahi", "nahin", "aur", "bhi", "kya", "kuch", "kuchh",
        "mera", "meri", "mere", "hamara", "hamare", "apna", "apni",
        "abhi", "jaldi", "thoda", "thodi", "zyada", "sasta", "sasti", "mehenga", "mehngi",
        "wala", "wali", "wale", "mein", "liye", "saath", "sab",
        "achha", "accha", "theek", "haan", "bhugtan", "saman", "samaan", "dukaan",
        "ka", "ki", "ke", "ko", "se",
    }
)  # fmt: skip

_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z]+")


def detect_language(text: str) -> Language:
    """Classify the buyer's message. Deterministic; never consults a model."""
    if has_devanagari(text):
        return Language.HI
    words = set(_WORD.findall(text.casefold()))
    if words & _HINGLISH_MARKERS:
        return Language.HI_LATN
    return Language.EN
