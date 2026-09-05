"""The outbound content guard (19.9, 19.10).

The model may converse about products, comparisons and choices. It does not author speech
about amounts, payment outcomes, refunds, approvals, mandates or reservations: those are
rendered from templates with server-confirmed fields and carry ``deterministic=True``.
Any model-authored sentence that states such a fact is refused before synthesis, which is
the whole reason the pipeline is split: text exists before speech, so a sentence can be
refused rather than recalled.

The guard evaluates exactly the units the chunker will speak, using the same tokenizer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .tokenizer import split_sentences

#: Currency symbols, codes, rupee/paise words, Indian-grouped or two-decimal amounts.
MONEY_FACT: Final[re.Pattern[str]] = re.compile(
    r"₹|\bINR\b|\bRs\.?\s*\d|\brupees?\b|\bpaise?\b"
    r"|\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\b\d+\.\d{2}\b"
    r"|रुपये|रुपए|पैसे",
    re.IGNORECASE,
)

#: Payment, refund, approval, mandate, reservation and cancellation outcomes.
TRANSACTION_OUTCOME: Final[re.Pattern[str]] = re.compile(
    r"\b(?:refund(?:ed|s|ing)?|captured?|authori[sz]ed|payment\s+(?:is\s+)?"
    r"(?:failed|pending|unknown|succeeded|successful|completed?|done|authori[sz]ed)"
    r"|approv(?:ed|al)|cancell?(?:ed|ation)|mandate|revok(?:ed|e)|reservation"
    r"|expir(?:ed|es|y))\b"
    r"|भुगतान|रिफ़ंड|रिफंड|स्वीकृति|रद्द|आदेश",
    re.IGNORECASE,
)


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

    def check(self, text: str, *, deterministic: bool) -> GuardVerdict:
        """Deterministic (template) speech passes whole; model text is checked per sentence."""
        sentences = split_sentences(text)
        if deterministic:
            return GuardVerdict(allowed=tuple(sentences), refused=())
        allowed: list[str] = []
        refused: list[Refusal] = []
        for sentence in sentences:
            reason = self.reason_to_refuse(sentence)
            if reason is None:
                allowed.append(sentence)
            else:
                refused.append(Refusal(sentence=sentence, reason=reason))
        return GuardVerdict(allowed=tuple(allowed), refused=tuple(refused))

    @staticmethod
    def reason_to_refuse(sentence: str) -> str | None:
        if MONEY_FACT.search(sentence):
            return "money_fact_outside_template"
        if TRANSACTION_OUTCOME.search(sentence):
            return "transaction_outcome_outside_template"
        return None
