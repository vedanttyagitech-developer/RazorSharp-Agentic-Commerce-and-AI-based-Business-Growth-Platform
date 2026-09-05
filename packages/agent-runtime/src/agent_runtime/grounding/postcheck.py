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

from ..core.fencing import plain
from ..language import Language
from .ledger import GroundingLedger

__all__ = ["ReplyCheck", "extract_amounts_minor", "extract_skus", "verify_reply"]

#: Catalogue identifiers look like ``AMUL-DAIRY-001``. Anything of that shape in a reply
#: is treated as a product reference and must be grounded.
_SKU: Final[re.Pattern[str]] = re.compile(r"\b[A-Z]{2,6}-[A-Z]{2,8}-\d{2,4}\b")

#: A rupee figure, grouped (``1,25,000``) or plain (``1250``).
#:
#: The grouped branch requires at least one comma and the whole pattern is followed by a
#: "no more digits" guard. Both are load-bearing. With ``(?:,\d{2,3})*`` the first branch
#: succeeded on the first three digits of ``1250`` and the alternation never reached
#: ``\d+``, so ``₹1250`` parsed as ₹125.00: a model could state a ten-times-wrong total
#: and have it *grounded* by the correct ₹125.00 fact, while the truthful ``₹1250.00``
#: was dropped as unproven. The suffix form (``1250 rupees``) was always correct, because
#: the trailing word forced the backtrack -- which is why every existing test passed.
_NUMBER: Final[str] = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?!\d)"
#: ``रु`` ends in a vowel sign (category Mn), which Python's ``\w`` excludes, so a
#: trailing ``\b`` demanded a word character to its *right* and could never match before a
#: space or a full stop. The suffix was dead for two languages. This is the same
#: Devanagari word-boundary defect ``core/grounding_rules.py`` fixed with ``_WORDISH``;
#: the fix had not been carried across.
_AMOUNT: Final[re.Pattern[str]] = re.compile(
    rf"(?:₹|Rs\.?|INR|रु\.?|रुपये|रूपये)\s*{_NUMBER}"
    rf"|{_NUMBER}\s*(?:/-)?\s*(?:rupees?|rupaye|rupiye|रुपये|रूपये|रु(?![\wऀ-ॿ]))",
    re.IGNORECASE,
)

#: The subject of a success claim, and the words that assert it succeeded. Kept as two
#: small alternations rather than one list of whole phrases: enumerating phrasings is how
#: ``payment successfully completed`` slipped through (``\b`` after ``successful`` fails
#: on the ``ly``), along with "has gone through", "the charge went through" and
#: "transaction successful". Every quantifier here is bounded, so the pattern stays linear.
_CLAIM_SUBJECT: Final[str] = r"(?:payment|transaction|charge|bhugtan)"
_CLAIM_VERB: Final[str] = (
    r"(?:successful(?:ly)?|succeeded|completed?|done|received|captured|confirmed|processed)"
)
_SUCCESS_CLAIM: Final[re.Pattern[str]] = re.compile(
    rf"\b{_CLAIM_SUBJECT}\s+"
    r"(?:was\s+|is\s+|has\s+been\s+|have\s+been\s+|got\s+|successfully\s+)?"
    rf"{_CLAIM_VERB}\b"
    r"|\bpaid\s+successfully\b"
    r"|\breceived\s+your\s+(?:payment|money)\b"
    # "your payment has gone through", "the charge went through"
    rf"|\b(?:{_CLAIM_SUBJECT}|money|amount)\b[^.!?।\n]{{0,40}}?\b(?:gone|went)\s+through\b"
    r"|\bmoney\s+(?:has\s+been\s+|was\s+)?(?:debited|deducted|taken)\b"
    r"|\bpayment\s+(?:ho\s+gay[ai]|safal|complete\s+ho\s+gay[ai])\b"
    r"|\bbhugtan\s+(?:safal|ho\s+gaya|poora)\b"
    r"|\bpais[ae]\s+(?:cut|kat|deduct\s+ho)\s+gay[ae]\b"
    r"|भुगतान\s+(?:सफल|हो\s+गया|पूरा)"
    r"|पैस[ाे]\s+(?:कट|काट)\s+गय[ाे]"
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
    """Catalogue-shaped identifiers in ``text``, in order, de-duplicated.

    Normalised first, so a zero-width space inside an identifier cannot hide it.
    """
    seen: dict[str, None] = {}
    for match in _SKU.finditer(plain(text)):
        seen.setdefault(match.group(0), None)
    return tuple(seen)


def extract_amounts_minor(text: str, currency: str = "INR") -> tuple[int, ...]:
    """Currency-marked amounts in ``text`` as integer minor units. Decimal, never float."""
    scale = Decimal(10) ** exponent_for(currency)
    found: dict[int, None] = {}
    # Normalised here as well as in ``verify_reply``: these two are exported, and a helper
    # that answers "the amounts in this text" must not answer differently because the text
    # carried a character nobody can see.
    for match in _AMOUNT.finditer(plain(text)):
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


def _asserts_anything_unproven(text: str, ledger: GroundingLedger, currency: str) -> bool:
    """Does this text still say something the ledger cannot prove? The output invariant."""
    if any(not ledger.knows_sku(sku) for sku in extract_skus(text)):
        return True
    if any(not ledger.knows_amount(minor) for minor in extract_amounts_minor(text, currency)):
        return True
    return bool(_SUCCESS_CLAIM.search(text)) and not ledger.payment_captured()


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
    # Scan the normalised text, never the raw. A zero-width space is category Cf, so it is
    # not ``\s`` and ``\s*`` steps straight over it: ``₹\u200b1,250.00`` reads as a
    # rupee sign followed by digits to every human and to no regex here. The fence already
    # takes this view of merchant prose; the model's own prose needs it for the same
    # reason. A reply with nothing wrong is returned exactly as it arrived.
    scanned = plain(reply)
    ungrounded_skus = tuple(sku for sku in extract_skus(scanned) if not ledger.knows_sku(sku))
    ungrounded_amounts = tuple(
        minor
        for minor in extract_amounts_minor(scanned, currency)
        if not ledger.knows_amount(minor)
    )
    success_claim = bool(_SUCCESS_CLAIM.search(scanned)) and not ledger.payment_captured()

    if not ungrounded_skus and not ungrounded_amounts and not success_claim:
        return ReplyCheck(reply, False, (), (), False, ())

    sentences = _sentences(scanned)
    kept: list[str] = []
    dropped: list[str] = []
    for sentence in sentences:
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
    # The per-sentence rescan can disagree with the reply-level scan. ``_AMOUNT`` binds a
    # marker to its digits with ``\s*``, which matches a line break, while
    # ``_SENTENCE_SPLIT`` splits on one: "₹\n1250" is an amount to the whole-reply scan
    # and to neither fragment, so nothing was dropped and the invented figure survived --
    # in a reply the caller was told had been rewritten. The same held for a success claim
    # broken across a line. Rather than enumerate the ways a split can disagree with a
    # match, assert the property that matters on the way out: what this returns must not
    # still assert something unproven. When it does, nothing survives, and the harness
    # renders its deterministic fallback.
    if _asserts_anything_unproven(rewritten, ledger, currency):
        dropped = list(sentences)
        rewritten = ""
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
