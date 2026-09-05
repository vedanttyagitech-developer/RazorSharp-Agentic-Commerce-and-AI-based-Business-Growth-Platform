"""The outbound content guard (19.9, 19.10).

The model may converse about products, comparisons and choices. It does not author speech
about approvals, totals, deltas, reservation expiry, payment outcomes, cancellation
effects, refunds or delegated authority: those sentences are rendered from versioned
templates filled with server-confirmed fields and carry ``deterministic=True``. A
model-authored sentence that states such a fact is refused **before synthesis**, which is
the whole reason the pipeline is split: text exists before speech, so a sentence can be
refused rather than recalled. "I have already refunded you" cannot be un-said.

The guard evaluates exactly the units the chunker will speak, using the same tokenizer, by
import rather than by copy. If a guard and the thing it guards tokenize differently, that
difference is the bypass.

TWO KINDS OF MONEY, AND WHY ONLY ONE IS BANNED
----------------------------------------------
Specification 19.10 lists *transactional* money: the total being approved, a material
price or fee delta, a refund amount, a captured amount. Those are never model prose. A
product's list price quoted back during browsing -- "the 1 litre Amul Gold is ₹73" -- is
not on that list, and a guard that refuses it silently gags the shopping conversation the
voice surface exists to have.

So an amount is spoken only when it is **grounded**: present, to the paisa, in the set of
integer minor units a tool result of this turn actually returned. A hallucinated price is
refused; a quoted one is not. That is a stronger rule than a blanket ban in the way that
matters, because the danger was never that a price was spoken -- it was that a price
nothing computed was spoken. Amounts are compared as integers throughout; no float touches
this path.

An empty grounded set means no tool returned money this turn, so every amount in model
prose is ungrounded and every such sentence is refused. That is the safe default, and it
is what a caller that omits the set gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from .tokenizer import split_sentences

__all__ = [
    "MONEY_FACT",
    "TRANSACTION_OUTCOME",
    "GuardVerdict",
    "Refusal",
    "SpeechGuard",
    "amounts_in",
]

#: Anything that reads as money: a currency mark, a rupee/paise word, or a grouped or
#: two-decimal figure. Matching widely is deliberate -- a hit only means "this sentence
#: talks about money", which sends it to the grounding check rather than refusing it.
MONEY_FACT: Final[re.Pattern[str]] = re.compile(
    r"₹|\bINR\b|\bRs\.?\s*\d|\brupees?\b|\bpaise?\b"
    r"|\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\b\d+\.\d{2}\b"
    r"|रुपये|रुपए|पैसे",
    re.IGNORECASE,
)

#: Payment, refund, approval, mandate, reservation and cancellation outcomes. These are
#: refused outright in model prose: unlike a price there is no "grounded" version of
#: "your refund is complete" that a model may author, because the sentence IS the claim.
TRANSACTION_OUTCOME: Final[re.Pattern[str]] = re.compile(
    r"\b(?:refund(?:ed|s|ing)?|captured?|authori[sz]ed|payment\s+(?:is\s+)?"
    r"(?:failed|pending|unknown|succeeded|successful|completed?|done|authori[sz]ed)"
    r"|approv(?:ed|al)|cancell?(?:ed|ation)|mandate|revok(?:ed|e)|reservation"
    r"|expir(?:ed|es|y))\b"
    r"|भुगतान|रिफ़ंड|रिफंड|स्वीकृति|रद्द|आदेश",
    re.IGNORECASE,
)

#: A number that reads as money, and ONLY when a currency marker sits beside it. A bare
#: integer in prose is usually not an amount -- "1 litre", "2 packs", "500 ml" -- and
#: treating it as one refused every ordinary product sentence. The marker may lead
#: (``₹73``, ``Rs. 73``, ``INR 73``) or trail (``73 rupees``, ``73 रुपये``).
#:
#: A figure with no marker at all therefore yields no amount. Such a sentence still trips
#: :data:`MONEY_FACT` if it is shaped like money, and a sentence that talks about money
#: while naming no checkable figure is refused -- so the untagged case fails closed.
_FIGURE: Final[str] = r"\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"

_RUPEES_LEADING: Final[re.Pattern[str]] = re.compile(
    rf"(?:₹|\bRs\.?|\bINR)\s*({_FIGURE})", re.IGNORECASE
)
#: The marker may also trail. RazorAI renders a product price as ``79.00 INR`` -- found by
#: speaking to the running system, not by reading the renderer -- and a guard that knew
#: only the leading form refused a correctly grounded price as unverifiable.
_RUPEES_TRAILING: Final[re.Pattern[str]] = re.compile(
    rf"({_FIGURE})\s*(?:INR\b|Rs\.?\B|rupees?|रुपये|रुपए)", re.IGNORECASE
)
_PAISE_TRAILING: Final[re.Pattern[str]] = re.compile(rf"({_FIGURE})\s*(?:paise?|पैसे)", re.IGNORECASE)

#: Minor units per major unit for INR. Voice is INR-only in P0; a second currency needs a
#: second decision about how it is spoken, not just a different exponent.
_MINOR_PER_MAJOR: Final[int] = 100


def _scaled(raw: str, factor: int) -> int | None:
    """``raw`` scaled to integer minor units, or ``None`` if it is not a whole number.

    Read through :class:`~decimal.Decimal`, never a float: parsing an amount through a
    float is how a paisa goes missing between the screen and the speaker.
    """
    try:
        value = Decimal(raw.replace(",", "")) * factor
    except InvalidOperation:  # pragma: no cover - the pattern admits only decimals
        return None
    return int(value) if value == value.to_integral_value() else None


def amounts_in(sentence: str) -> frozenset[int]:
    """Every currency-marked figure in ``sentence`` as integer minor units, read exactly."""
    found: set[int] = set()
    for pattern, factor in (
        (_RUPEES_LEADING, _MINOR_PER_MAJOR),
        (_RUPEES_TRAILING, _MINOR_PER_MAJOR),
        (_PAISE_TRAILING, 1),
    ):
        for match in pattern.finditer(sentence):
            minor = _scaled(match.group(1), factor)
            if minor is not None:
                found.add(minor)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Refusal:
    sentence: str
    reason: str


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """What may be spoken, in chunker order, and what was refused with why."""

    allowed: tuple[str, ...]
    refused: tuple[Refusal, ...]

    @property
    def refused_any(self) -> bool:
        return bool(self.refused)


class SpeechGuard:
    """Sentence-level guard over model-authored text."""

    #: Identity with the chunker's tokenizer is asserted by test, not assumed.
    tokenizer = staticmethod(split_sentences)

    def check(
        self,
        text: str,
        *,
        deterministic: bool,
        grounded_amounts_minor: frozenset[int] = frozenset(),
    ) -> GuardVerdict:
        """Template speech passes whole; model text is checked sentence by sentence."""
        sentences = split_sentences(text)
        if deterministic:
            # Rendered from server-confirmed fields by templates.py, which no model can
            # reach. There is nothing here for a guard to second-guess.
            return GuardVerdict(allowed=tuple(sentences), refused=())
        allowed: list[str] = []
        refused: list[Refusal] = []
        for sentence in sentences:
            reason = self.reason_to_refuse(sentence, grounded_amounts_minor)
            if reason is None:
                allowed.append(sentence)
            else:
                refused.append(Refusal(sentence=sentence, reason=reason))
        return GuardVerdict(allowed=tuple(allowed), refused=tuple(refused))

    @staticmethod
    def reason_to_refuse(
        sentence: str, grounded_amounts_minor: frozenset[int] = frozenset()
    ) -> str | None:
        """Why this sentence may not be spoken by a model, or ``None`` if it may."""
        if TRANSACTION_OUTCOME.search(sentence):
            return "transaction_outcome_outside_template"
        if MONEY_FACT.search(sentence):
            spoken = amounts_in(sentence)
            if not spoken:
                # Talks about money without naming a figure -- "that is cheaper" is fine,
                # but "the total in rupees" without a number is a claim we cannot check.
                return "money_fact_without_amount"
            ungrounded = spoken - grounded_amounts_minor
            if ungrounded:
                return "ungrounded_amount"
        return None
