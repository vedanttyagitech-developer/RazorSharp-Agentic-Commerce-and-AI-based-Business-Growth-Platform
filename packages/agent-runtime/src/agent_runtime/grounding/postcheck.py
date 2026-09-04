"""Reply post-check: prove every SKU and amount in the answer came from a tool.

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

from ..language import Language
from .ledger import GroundingLedger

__all__ = ["ReplyCheck", "extract_amounts_minor", "extract_skus", "verify_reply"]

#: Catalogue identifiers look like ``GRO-DAIRY-001``. Anything of that shape in a reply
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


@dataclass(frozen=True, slots=True)
class ReplyCheck:
    """Outcome of the post-check. ``reply`` is safe to show; ``rewritten`` says if it changed."""

    reply: str
    rewritten: bool
    ungrounded_skus: tuple[str, ...]
    ungrounded_amounts_minor: tuple[int, ...]
    success_claim_removed: bool
    dropped_sentences: tuple[str, ...]


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
    success_claim = bool(_SUCCESS_CLAIM.search(reply)) and not ledger.payment_captured()

    if not ungrounded_skus and not ungrounded_amounts and not success_claim:
        return ReplyCheck(reply, False, (), (), False, ())

    kept: list[str] = []
    dropped: list[str] = []
    for sentence in _sentences(reply):
        bad_sku = any(sku in sentence for sku in ungrounded_skus)
        bad_amount = any(
            minor in ungrounded_amounts for minor in extract_amounts_minor(sentence, currency)
        )
        bad_claim = success_claim and bool(_SUCCESS_CLAIM.search(sentence))
        if bad_sku or bad_amount or bad_claim:
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
    )
