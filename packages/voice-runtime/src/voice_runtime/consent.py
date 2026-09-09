"""Spoken consent: a closed lexicon and a bounded window. No model, no I/O, no network.

The Approve button records consent against the exact bytes on the approval card. Voice is
a second way to press that button, and this module is the part that decides whether a
settled transcript IS a press. It is deliberately small and deliberately plain code:

* The lexicon is a **set**, not a classifier. A transcript counts as a yes when every word
  in it is in the affirmative or companion sets and at least one is affirmative; as a no
  when any word is negative; and as nothing otherwise. "yes please" is a yes. "yes please
  do it now" is nothing, because "it" and "now" are outside the sets. "bilkul" alone is
  nothing, because companions are never sufficient. There is no scoring and no threshold.

* The window is a **time bound anchored on the server's own send-completion**. A yes before
  the amount was read is not consent to that amount; a yes long after is stale. The window
  opens when the gateway has finished sending the reading and closes ``CONSENT_WINDOW_S``
  later, on the pipeline's monotonic clock. Nothing the client sends extends it.

* A window is **single-use** and bound to one card. It is consumed by the first yes or no
  inside it, closed by a barge-in, superseded by the next reading, and it carries the
  checkout id, version, content hash and amount that were spoken, so a recognised yes can
  only ever refer to the card the buyer just heard.

Recognised is not recorded. This module produces a fact -- "the buyer said yes to these
bytes inside this window" -- and the trusted surface decides what to do with it, by
sending the same request the button sends. ``tests/test_voice_consent.py`` asserts that
this module imports no model SDK and never can.

TOKENISATION
------------
The same rule as ``commerce_api.services.agent_service._tokens``: NFKC first, then
casefold, words matched by ``[\\wऀ-ॿ]+``. NFKC is what makes the precomposed nukta letter
``ज़`` (U+095B) and the decomposed pair ``ज`` + U+093C the same entry, and folds fullwidth
``ｙｅｓ`` to ASCII. The table entries are normalised the same way at import, so the lexicon
cannot disagree with the tokenizer about what a word looks like. One addition the
recognizer forces: the Devanagari danda is stripped first, because it sits inside the block
that pattern admits and a spoken "हाँ।" must be the word "हाँ".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from .clock import Clock
from .constants import CONSENT_WINDOW_S
from .stt.transcript import TranscriptTurn

__all__ = [
    "AFFIRMATIVE",
    "COMPANION",
    "MAX_CONSENT_TOKENS",
    "NEGATIVE",
    "ApprovalCardFacts",
    "CardReader",
    "CardUnavailableError",
    "ConsentObservation",
    "ConsentTracker",
    "ConsentWindow",
    "Verdict",
    "classify",
    "tokens",
]

Verdict = Literal["yes", "no", "none"]

#: The most words a consent utterance may have. Four covers "haan ji bilkul please" and
#: refuses a sentence: consent is a word, not a paragraph.
MAX_CONSENT_TOKENS: Final[int] = 4

_WORDS: Final[re.Pattern[str]] = re.compile(r"[\wऀ-ॿ]+")
#: The danda and double danda are punctuation, but they sit inside the Devanagari block
#: the word pattern admits, so "हाँ।" would otherwise be one token that is not "हाँ". The
#: recognizer appends a danda to most settled Hindi finals; they are stripped first.
_DANDAS: Final[dict[int, str]] = {0x0964: " ", 0x0965: " "}


def _fold(word: str) -> str:
    return unicodedata.normalize("NFKC", word).casefold()


def _table(*words: str) -> frozenset[str]:
    """A lexicon table, normalised exactly as the tokenizer normalises input."""
    return frozenset(_fold(word) for word in words)


#: Words that, on their own or with companions, mean yes. English, Devanagari Hindi and
#: romanised Hinglish, because the buyer speaks all three and a lexicon that covers only
#: one of them covers none. ``ok``, ``okay``, ``theek`` and ``ठीक`` are deliberately absent:
#: they acknowledge, they do not consent. ``sahi`` and ``correct`` likewise.
AFFIRMATIVE: Final[frozenset[str]] = _table(
    # English
    "yes", "yeah", "yep", "approve", "approved", "confirm",
    # Hindi
    "हाँ", "हां", "मंज़ूर", "मंजूर", "स्वीकार",
    # Hinglish
    "haan", "han", "haa", "manzoor", "manjoor",
)  # fmt: skip

#: Words that may accompany a yes without changing it, and are never sufficient alone.
#: ``ji`` alone is also "pardon?", which is exactly why it is here and not above.
COMPANION: Final[frozenset[str]] = _table(
    "please", "ji", "जी", "bilkul", "बिल्कुल", "kar", "karo", "do", "कर", "करो", "दो",
    "hai", "है",
)  # fmt: skip

#: Words that decline. Any one of them anywhere in the utterance wins: "haan na" is a
#: Hindi tag question, and it is refused on purpose, because a false decline never costs
#: money and a false approval does.
NEGATIVE: Final[frozenset[str]] = _table(
    # English. "don't" tokenises as "don" + "t", so "don" is the entry.
    "no", "nope", "dont", "don", "reject", "cancel", "stop", "wait",
    # Hindi
    "नहीं", "नही", "ना", "मत", "रुको", "रद्द",
    # Hinglish
    "nahi", "nahin", "nai", "na", "mat", "ruko", "raddi",
)  # fmt: skip

# A word in two tables would let the rule below give two answers. Refused at import.
if AFFIRMATIVE & NEGATIVE or COMPANION & NEGATIVE or AFFIRMATIVE & COMPANION:
    raise RuntimeError("the consent lexicon tables overlap")


def tokens(text: str) -> list[str]:
    """``text`` as comparable words: NFKC, then casefold, split on non-word characters."""
    normalized = unicodedata.normalize("NFKC", text).translate(_DANDAS)
    return [token.casefold() for token in _WORDS.findall(normalized)]


def classify(text: str) -> Verdict:
    """The rule, in the order it is applied.

    1. Any negative word -> ``no``.
    2. One to four words, all affirmative or companion, at least one affirmative -> ``yes``.
    3. Anything else -> ``none``.

    Step 3 is where a model's sentence lands. "Say yes to approve" carries "say" and "to";
    "Yes, approved, done!" carries "done". Neither is a subset of the tables, so neither is
    consent, whoever said it.
    """
    words = tokens(text)
    if not words:
        return "none"
    if any(word in NEGATIVE for word in words):
        return "no"
    if len(words) > MAX_CONSENT_TOKENS:
        return "none"
    if all(word in AFFIRMATIVE or word in COMPANION for word in words) and any(
        word in AFFIRMATIVE for word in words
    ):
        return "yes"
    return "none"


# ---- the card, as the gateway read it ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalCardFacts:
    """The five fields consent binds to, read from the trusted server, never from a client.

    This is exactly what the Approve button echoes back --
    ``POST /v1/checkouts/{id}/versions/{n}/approve`` with ``content_hash``, ``amount_minor``
    and ``currency`` -- plus the two path parameters. ``amount_minor`` is an integer number
    of minor units and is never divided, formatted or compared as anything else here.
    """

    checkout_id: str
    version: int
    content_hash: str
    amount_minor: int
    currency: str


class CardUnavailableError(RuntimeError):
    """The card could not be read as asked: unreachable, not owned, not awaiting approval,
    or a different version from the one on the screen. Surfaced as a visible degradation."""


class CardReader(Protocol):
    """Reads the approval card of one checkout, through the buyer's own credential."""

    async def read_card(self, checkout_id: str, version: int) -> ApprovalCardFacts: ...

    async def read_guidance(
        self, checkout_id: str, stage: str, version: int | None = None
    ) -> tuple[str, frozenset[int]]: ...


# ---- the window ---------------------------------------------------------------------------

CloseReason = Literal["recognised", "declined", "expired", "barge_in", "superseded"]


@dataclass(slots=True)
class ConsentWindow:
    """One bounded chance to say yes or no to one card."""

    consent_id: str
    card: ApprovalCardFacts
    speech_generation: int
    opened_at: float
    closes_at: float
    #: The voice turn that was already in flight when the window opened, if any. An
    #: utterance that BEGAN before the reading finished cannot finalise into consent: the
    #: buyer had not heard the whole amount when they started speaking.
    excluded_turn_id: int | None = None
    closed: CloseReason | None = field(default=None)

    @property
    def is_open(self) -> bool:
        return self.closed is None


ObservationKind = Literal[
    "no_window", "recognised", "declined", "unrecognised", "began_before_reading", "expired"
]


@dataclass(frozen=True, slots=True)
class ConsentObservation:
    """What a settled transcript meant for the current window, if there was one."""

    kind: ObservationKind
    verdict: Verdict
    window: ConsentWindow | None


class ConsentTracker:
    """Holds at most one window and applies the rule to settled transcripts.

    Only ``observe`` reads text, and it is only ever handed a :class:`TranscriptTurn` that
    the recognizer settled from the buyer's microphone. A reply from the agent, a template
    the gateway spoke, or typed text never comes through here; the pipeline has no call
    site that could pass them.
    """

    def __init__(self, *, clock: Clock, window_s: float = CONSENT_WINDOW_S) -> None:
        self._clock = clock
        self._window_s = window_s
        self._window: ConsentWindow | None = None
        # Metrics (19.13)
        self.windows_opened = 0
        self.recognised = 0
        self.declined = 0
        self.expired = 0
        self.near_misses = 0

    @property
    def window_s(self) -> float:
        return self._window_s

    @property
    def open_window(self) -> ConsentWindow | None:
        window = self._window
        return window if window is not None and window.is_open else None

    def open(
        self,
        *,
        consent_id: str,
        card: ApprovalCardFacts,
        speech_generation: int,
        excluded_turn_id: int | None,
    ) -> ConsentWindow:
        """Open a window now. A window still open is superseded, never stacked."""
        self.close("superseded")
        now = self._clock.now()
        window = ConsentWindow(
            consent_id=consent_id,
            card=card,
            speech_generation=speech_generation,
            opened_at=now,
            closes_at=now + self._window_s,
            excluded_turn_id=excluded_turn_id,
        )
        self._window = window
        self.windows_opened += 1
        return window

    def close(self, reason: CloseReason) -> ConsentWindow | None:
        """Close the open window, if any, and return it so the closure can be sent."""
        window = self.open_window
        if window is None:
            return None
        window.closed = reason
        if reason == "expired":
            self.expired += 1
        return window

    def close_if(self, consent_id: str, reason: CloseReason) -> ConsentWindow | None:
        """Close only if the open window is still the one named. For the expiry timer,
        which must not close a newer window that replaced the one it was started for."""
        window = self.open_window
        if window is None or window.consent_id != consent_id:
            return None
        return self.close(reason)

    def observe(self, turn: TranscriptTurn) -> ConsentObservation:
        """Apply the rule to one settled voice transcript.

        A yes or no inside the window consumes it. A near-miss leaves it open: the buyer
        may still answer. A transcript outside the window, or whose audio is older than the
        window, expires it visibly rather than counting.
        """
        window = self.open_window
        if window is None:
            return ConsentObservation("no_window", "none", None)
        verdict = classify(turn.text)
        now = self._clock.now()
        inside = (
            window.opened_at <= turn.stamp.observed_at <= window.closes_at
            and now <= window.closes_at
            and turn.stamp.audio_age_s <= self._window_s
        )
        if not inside:
            self.close("expired")
            return ConsentObservation("expired", verdict, window)
        if window.excluded_turn_id is not None and turn.turn_id == window.excluded_turn_id:
            self.near_misses += 1
            return ConsentObservation("began_before_reading", verdict, window)
        if verdict == "yes":
            self.recognised += 1
            self.close("recognised")
            return ConsentObservation("recognised", verdict, window)
        if verdict == "no":
            self.declined += 1
            self.close("declined")
            return ConsentObservation("declined", verdict, window)
        self.near_misses += 1
        return ConsentObservation("unrecognised", verdict, window)
