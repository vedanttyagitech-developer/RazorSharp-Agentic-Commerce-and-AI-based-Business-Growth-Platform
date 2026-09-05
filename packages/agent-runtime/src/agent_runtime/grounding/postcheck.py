"""Reply post-check: prove every SKU, amount and unit count in the answer came from a tool.

The model writes prose; this module reads it back against the
:class:`~agent_runtime.grounding.ledger.GroundingLedger` and rewrites what it cannot
prove. Three checks, all lexical and deterministic:

1. **SKUs.** Any catalogue-shaped identifier not returned this turn is an unverified
   product (specification 20.4): the sentence is dropped, the buyer is told the item
   could not be verified, and grounded alternatives are offered -- by name and SKU, from
   the ledger, never invented.
2. **Amounts.** Any currency-marked number that is not a money fact in the ledger is
   model arithmetic or invention. The sentence is dropped. A wrong total in prose is the
   first link of a wrong charge.
3. **Success claims.** "Payment successful" is permitted only when a structured read
   reported ``CAPTURED``. Admission is not payment; a browser redirect is not capture.
4. **Unit counts.** "Only 2 left" carries no currency mark, so checks 1 and 2 read straight
   past it, and it is the cheapest scarcity claim there is: a shelf count is the number a
   buyer reads as a reason to hurry. A remaining-count in prose must be a count a merchant
   read returned this turn, or the sentence goes.
5. **Sales pressure.** "Selling fast", "best seller", "everyone is buying", "jaldi
   kijiye" -- demand and popularity claims with no number in them at all. No tool on this
   platform returns a demand signal, a sales rank or a popularity figure; nothing records
   units sold per product anywhere. These are ungroundable by construction rather than
   ungrounded by accident, so they are removed without consulting the ledger.

Checks 4 and 5 are the buyer-facing half of the same rule the storefront keeps: the shop
does not tell a buyer that other people bought something in order to make them likelier to
buy it. A prompt already forbids both (``shopping_specialist.md``), and a prompt is a
request. This module is the part that holds when the model does not.

Rewriting is by sentence, not by word: patching a number inside a sentence would leave
the model's reasoning around it intact and now wrong. When nothing survives, ``reply`` is
empty and the harness renders the deterministic fallback (``render_fallback``) so the
buyer learns something was withheld rather than receiving silence; the harness owns that
choice because it also records the correction.

The third line of defence (ADR 0004 section 1.2): the fence stops the model from being
told a lie, the provenance gate stops it from acting on one, and this stops it from
repeating one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from commerce_domain import exponent_for

from ..backends.memory import LOW_STOCK_UNITS
from ..language import Language
from .ledger import GroundingLedger

__all__ = [
    "ReplyCheck",
    "extract_amounts_minor",
    "extract_skus",
    "extract_stock_counts",
    "verify_reply",
]

#: Catalogue identifiers look like ``AMUL-DAIRY-001``. Anything of that shape in a reply
#: is treated as a product reference and must be grounded.
_SKU: Final[re.Pattern[str]] = re.compile(r"\b[A-Z]{2,6}-[A-Z]{2,8}-\d{2,4}\b")

_NUMBER: Final[str] = r"(\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_AMOUNT: Final[re.Pattern[str]] = re.compile(
    rf"(?:₹|Rs\.?|INR|रु\.?|रुपये|रूपये)\s*{_NUMBER}"
    rf"|{_NUMBER}\s*(?:/-)?\s*(?:rupees?|rupaye|rupiye|रुपये|रूपये|रु\b)",
    re.IGNORECASE,
)

_SUCCESS_CLAIM: Final[re.Pattern[str]] = re.compile(
    r"\bpayment\s+(?:was\s+|is\s+|has\s+been\s+|got\s+)?"
    r"(?:successful|succeeded|complete|completed|done|received|captured|confirmed)\b"
    r"|\bpaid\s+successfully\b"
    r"|\bpayment\s+(?:ho\s+gay[ai]|safal|complete\s+ho\s+gay[ai])\b"
    r"|\bbhugtan\s+(?:safal|ho\s+gaya|poora)\b"
    r"|भुगतान\s+(?:सफल|हो\s+गया|पूरा)"
    r"|\border\s+(?:has\s+been\s+|is\s+)?(?:confirmed|placed)\b",
    re.IGNORECASE,
)

#: Words that turn a bare number into a claim about what is left on a shelf. "Available"
#: is here on purpose: "only 2 available" is the same assertion as "only 2 left", and a
#: sentence carrying a unit figure at all is one this check wants to see grounded.
_REMAINING: Final[str] = (
    r"(?:left|remaining|in\s+stock|available|bach[aei]|bache?\s+h(?:ai|ain)"
    r"|बच[ेीा]|शेष|उपलब्ध)"
)

#: Nouns a count may be expressed in before the remaining-word arrives, so "3 units left"
#: and "3 packets bache hain" are read the same way as "3 left".
_UNIT_NOUN: Final[str] = (
    r"(?:units?|pcs?|pieces?|packs?|packets?|bottles?|boxes|items?|nos?\.?"
    r"|यूनिट|पैकेट|टुकड़े|डिब्बे)"
)

#: Small numbers spelled out. A model writing a scarcity line reaches for "only two left"
#: about as readily as "only 2 left", and a check that reads digits alone is one rewording
#: away from being bypassed. Ten is the ceiling: beyond it prose uses digits, and a
#: scarcity claim lives at the low end by its nature. Devanagari digits need no entry here
#: -- Python's ``\d`` and ``int`` both already read २ as two.
#: Grouped by value across the three scripts a reply may be written in, so a number is
#: read once and every spelling of it stays visibly together.
_NUMBER_WORDS: Final[tuple[tuple[int, tuple[str, ...]], ...]] = (
    (1, ("one", "ek", "एक")),
    (2, ("two", "do", "दो")),
    (3, ("three", "teen", "तीन")),
    (4, ("four", "char", "chaar", "चार")),
    (5, ("five", "paanch", "panch", "पांच", "पाँच")),
    (6, ("six", "chah", "cheh", "छह")),
    (7, ("seven", "saat", "सात")),
    (8, ("eight", "aath", "आठ")),
    (9, ("nine", "nau", "नौ")),
    (10, ("ten", "das", "दस")),
)

_WORD_NUMBERS: Final[Mapping[str, int]] = {
    word: value for value, words in _NUMBER_WORDS for word in words
}

_COUNT: Final[str] = rf"(\d{{1,4}}|{'|'.join(_WORD_NUMBERS)})"

#: A count of what remains, in either order: the number before the remaining-word
#: ("2 units left", "सिर्फ़ 2 बचे"), or after it ("in stock: 2", "stock mein 2 hain").
#: Up to two filler words are tolerated between the two halves -- "2 units are still left"
#: -- and no more, so a count in one clause cannot bind to a stock word in the next.
_STOCK_CLAIM: Final[re.Pattern[str]] = re.compile(
    rf"\b{_COUNT}\s+(?:{_UNIT_NOUN}\s+)?(?:\w+\s+){{0,2}}?{_REMAINING}\b"
    rf"|\b(?:in\s+stock|stock\s+mein|स्टॉक\s+में)\s*[:\-]?\s*"
    rf"(?:hai|hain|है|हैं)?\s*{_COUNT}\b",
    re.IGNORECASE | re.UNICODE,
)

#: Demand, popularity and urgency, asserted without a number. Nothing on this platform
#: records units sold, a sales rank, a view count or a demand curve, so there is no read
#: that could ever make one of these true; unlike a count, they are not checked against the
#: ledger because no ledger entry could exist. Kept as an explicit closed vocabulary rather
#: than a sentiment judgement, so what is removed is reviewable and nothing else is.
_PRESSURE: Final[re.Pattern[str]] = re.compile(
    r"\bhurry\b|\bselling\s+(?:out\s+)?fast\b|\bgoing\s+fast\b|\bflying\s+off\b"
    r"|\bsells\s+out\s+fast\b|\bwhile\s+stocks?\s+last\b|\bbefore\s+it'?s\s+gone\b"
    r"|\b(?:in\s+)?high\s+demand\b|\bhuge\s+demand\b|\bvery\s+much\s+in\s+demand\b"
    r"|\b(?:very|most|super|really)\s+popular\b|\bpopular\s+(?:choice|pick|item)\b"
    r"|\bbest[\s-]?sell(?:er|ers|ing)\b|\btop[\s-]?sell(?:er|ers|ing)\b"
    r"|\btrending\b|\bmost[\s-]?bought\b|\bcustomer\s+favou?rite\b|\bfan\s+favou?rite\b"
    r"|\beveryone\s+(?:is\s+buying|buys)\b|\bother\s+(?:buyers|customers)\s+(?:also\s+)?bought\b"
    r"|\bfrequently\s+bought\s+together\b|\bhighly\s+rated\b|\bcrowd\s+favou?rite\b"
    r"|\bdon'?t\s+miss\s+(?:out|it|this)\b|\bact\s+fast\b|\bgrab\s+it\s+(?:now|fast)\b"
    r"|\blimited[\s-]time\s+(?:offer|deal|only)\b"
    # Headcount social proof: "14 people bought it in the last hour", "500 sold today".
    # No read on this platform returns a buyer count, a view count or a sales total, so the
    # number is invented in every case and there is no ledger entry to check it against.
    r"|\b\d+\s+(?:people|persons?|buyers|customers|shoppers|others|log(?:on)?)\s+"
    r"(?:have\s+|has\s+|ne\s+)?(?:bought|ordered|purchased|added|khareed\w*)"
    r"|\b\d+\s+sold\b|\bsold\s+\d+\s+(?:today|this\s+week|in\s+the\s+last)"
    r"|\bjaldi\s+(?:kar|kij?iye|karo)|\bstock\s+khatam\s+ho\s+raha"
    r"|\bsabse\s+(?:zyada|jyada)\s+bik|\bbahut\s+(?:popular|demand)\b|\btezi\s+se\s+bik"
    r"|जल्दी\s+कर|स्टॉक\s+खत्म\s+हो\s+रहा|सबसे\s+ज़्?यादा\s+बिक"
    r"|बहुत\s+(?:लोकप्रिय|मांग)|तेज़?ी\s+से\s+बिक",
    re.IGNORECASE | re.UNICODE,
)

#: Scarcity with the number left out. Unlike :data:`_PRESSURE` this is not ungroundable --
#: a shelf really can be nearly empty, and a merchant asking which lines need restocking is
#: owed the phrase. What makes it a dark pattern is asserting it when no read said so, so it
#: is gated on the ledger rather than banned: allowed when some merchant read this turn came
#: back genuinely low, dropped when none did. The alternative, an unconditional ban, would
#: silence a true sentence about a real anomaly, which is its own kind of dishonesty.
_VAGUE_SCARCITY: Final[re.Pattern[str]] = re.compile(
    r"\balmost\s+(?:gone|out|sold\s+out)\b|\bnearly\s+(?:gone|sold\s+out)\b"
    r"|\brunning\s+(?:out|low)\b|\blast\s+few\b|\bonly\s+a\s+few\s+left\b"
    r"|\bkhatam\s+hone\s+wala|\bkam\s+bach[ae]\s+h(?:ai|ain)"
    r"|खत्म\s+होने\s+वाला|थोड़े\s+ही\s+बचे",
    re.IGNORECASE | re.UNICODE,
)

#: The platform's own definition of a low shelf, borrowed rather than re-picked so that
#: "running out" means in prose exactly what it means on an inventory anomaly card.
_LOW_STOCK_CEILING: Final[int] = LOW_STOCK_UNITS


def _saw_low_stock(ledger: GroundingLedger) -> bool:
    """Did any merchant read this turn come back low enough to call a shelf nearly empty?"""
    return any(0 < units <= _LOW_STOCK_CEILING for units in ledger.stock_counts)


_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?।])\s+|\n+")

#: Specification 20.4 step 4, said plainly in the buyer's language. ``{skus}`` is the
#: comma-joined list of identifiers that could not be verified.
_UNVERIFIED_ITEM: Final[Mapping[Language, str]] = {
    Language.EN: "I could not verify {skus} in this store's catalogue, so I have not included it.",
    Language.HI: "मैं {skus} को इस दुकान की सूची में सत्यापित नहीं कर सका, इसलिए उसे शामिल नहीं किया।",
    Language.HI_LATN: (
        "Main {skus} ko is dukaan ki list mein verify nahi kar saka, isliye use shaamil nahi kiya."
    ),
}

#: Step 3: grounded alternatives, offered by name and SKU. ``{items}`` is a joined list.
_ALTERNATIVES: Final[Mapping[Language, str]] = {
    Language.EN: "Available instead: {items}.",
    Language.HI: "इसके बदले उपलब्ध: {items}।",
    Language.HI_LATN: "Iske badle available: {items}.",
}

_MAX_ALTERNATIVES: Final[int] = 3


def extract_skus(text: str) -> tuple[str, ...]:
    """Catalogue-shaped identifiers in ``text``, in order, de-duplicated."""
    seen: dict[str, None] = {}
    for match in _SKU.finditer(text):
        seen.setdefault(match.group(0), None)
    return tuple(seen)


def extract_amounts_minor(text: str, currency: str = "INR") -> tuple[int, ...]:
    """Currency-marked amounts in ``text`` as integer minor units. Decimal, never float."""
    scale = Decimal(10) ** exponent_for(currency)
    found: dict[int, None] = {}
    for match in _AMOUNT.finditer(text):
        raw = next(group for group in match.groups() if group is not None)
        try:
            value = Decimal(raw.replace(",", "")) * scale
        except InvalidOperation:  # pragma: no cover - the regex only admits decimals
            continue
        if value == value.to_integral_value():
            found.setdefault(int(value), None)
    return tuple(found)


def extract_stock_counts(text: str) -> tuple[int, ...]:
    """Remaining-unit counts asserted in ``text``, in order, de-duplicated.

    A count is a whole number of things on a shelf, so word-numbers resolve through
    :data:`_WORD_NUMBERS` and digits through ``int`` -- which reads Devanagari digits too.
    """
    found: dict[int, None] = {}
    for match in _STOCK_CLAIM.finditer(text):
        raw = next((group for group in match.groups() if group is not None), None)
        if raw is None:  # pragma: no cover - every branch of the pattern captures
            continue
        spelled = _WORD_NUMBERS.get(raw.casefold())
        if spelled is not None:
            found.setdefault(spelled, None)
            continue
        try:
            found.setdefault(int(raw), None)
        except ValueError:  # pragma: no cover - the pattern admits digits or known words
            continue
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ReplyCheck:
    """Outcome of the post-check. ``reply`` is safe to show; ``rewritten`` says if it changed."""

    reply: str
    rewritten: bool
    ungrounded_skus: tuple[str, ...]
    ungrounded_amounts_minor: tuple[int, ...]
    success_claim_removed: bool
    dropped_sentences: tuple[str, ...]
    #: Remaining-unit counts no merchant read returned this turn.
    ungrounded_stock_counts: tuple[int, ...] = ()
    #: Whether a demand, popularity or urgency claim was taken out.
    pressure_removed: bool = False


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT.split(text) if part and part.strip()]


def _unverified_appendix(
    ungrounded_skus: tuple[str, ...], ledger: GroundingLedger, language: Language
) -> str:
    """The 20.4 sentences: what could not be verified, and what the merchant does have."""
    parts = [_UNVERIFIED_ITEM[language].format(skus=", ".join(ungrounded_skus))]
    alternatives = ledger.alternatives(_MAX_ALTERNATIVES)
    if alternatives:
        # Names here are merchant text that already passed the fence when the ledger
        # recorded them; the SKU beside each is what the buyer can act on.
        items = ", ".join(f"{p.name} ({p.sku})" for p in alternatives)
        parts.append(_ALTERNATIVES[language].format(items=items))
    return " ".join(parts)


def verify_reply(
    reply: str,
    ledger: GroundingLedger,
    *,
    currency: str = "INR",
    language: Language = Language.EN,
) -> ReplyCheck:
    """Drop every sentence that asserts something the ledger cannot prove.

    ``language`` chooses the script of the deterministic sentence this check *adds* (the
    unverified-item notice); it never changes what is removed.
    """
    ungrounded_skus = tuple(sku for sku in extract_skus(reply) if not ledger.knows_sku(sku))
    ungrounded_amounts = tuple(
        minor for minor in extract_amounts_minor(reply, currency) if not ledger.knows_amount(minor)
    )
    ungrounded_counts = tuple(
        units for units in extract_stock_counts(reply) if not ledger.knows_stock_count(units)
    )
    success_claim = bool(_SUCCESS_CLAIM.search(reply)) and not ledger.payment_captured()
    ungrounded_scarcity = not _saw_low_stock(ledger)
    pressure = bool(_PRESSURE.search(reply)) or (
        ungrounded_scarcity and bool(_VAGUE_SCARCITY.search(reply))
    )

    if (
        not ungrounded_skus
        and not ungrounded_amounts
        and not ungrounded_counts
        and not success_claim
        and not pressure
    ):
        return ReplyCheck(reply, False, (), (), False, ())

    kept: list[str] = []
    dropped: list[str] = []
    for sentence in _sentences(reply):
        bad_sku = any(sku in sentence for sku in ungrounded_skus)
        bad_amount = any(
            minor in ungrounded_amounts for minor in extract_amounts_minor(sentence, currency)
        )
        bad_count = any(units in ungrounded_counts for units in extract_stock_counts(sentence))
        bad_claim = success_claim and bool(_SUCCESS_CLAIM.search(sentence))
        # Pressure is judged per sentence rather than off the reply-level flag: unlike the
        # others it needs no ledger lookup, so a single urgent line cannot take a grounded
        # one down with it.
        bad_pressure = bool(_PRESSURE.search(sentence)) or (
            ungrounded_scarcity and bool(_VAGUE_SCARCITY.search(sentence))
        )
        if bad_sku or bad_amount or bad_count or bad_claim or bad_pressure:
            dropped.append(sentence)
        else:
            kept.append(sentence)

    rewritten = " ".join(kept).strip()
    if ungrounded_skus:
        rewritten = f"{rewritten} {_unverified_appendix(ungrounded_skus, ledger, language)}".strip()
    return ReplyCheck(
        reply=rewritten,
        rewritten=True,
        ungrounded_skus=ungrounded_skus,
        ungrounded_amounts_minor=ungrounded_amounts,
        success_claim_removed=success_claim,
        dropped_sentences=tuple(dropped),
        ungrounded_stock_counts=ungrounded_counts,
        pressure_removed=pressure,
    )
