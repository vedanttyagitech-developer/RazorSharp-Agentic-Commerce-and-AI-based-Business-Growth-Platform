"""Transcript state with REPLACE semantics and a freshness stamp (19.5, 19.4).

Streaming recognizers send the whole current hypothesis every frame and revise it freely.
Appending frames produces ``TumjoMainejoMaineTuMeriTuMainu``. The merge rule is one
expression, ``incoming or current``: replace rather than append, and an empty frame never
clears the held turn. The same rule governs both directions.

Only a final transcript may enter agent intent processing, and only while it is fresh.

Freshness is a property of the **audio**, not of the moment the text object was built.
Stamping a transcript with "now" and then comparing it to "now" is a check that can never
fail; what actually goes stale is the speech, when the recognizer is working through a
backlog after a reconnect. So a stamp carries ``audio_age_s`` -- how old the microphone
frame already was when it was finally sent -- and that is what the freshness window is
compared against. Specification 19.4: the buyer can repeat themselves, but they cannot
un-hear an answer to a question they abandoned twenty seconds ago.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..clock import Clock
from ..constants import TRANSCRIPT_FRESHNESS_S


def apply_stream_text(current: str, incoming: str | None) -> str:
    """``incoming or current``: replace, never append; empty never clears (19.5)."""
    return incoming or current


@dataclass(frozen=True, slots=True)
class FreshnessStamp:
    """When, from which connection generation, and off how old an audio frame."""

    observed_at: float
    generation: int
    #: How old the most recent microphone frame already was when the recognizer received
    #: it. Zero on a healthy stream; large when a reconnect left a backlog to work through.
    audio_age_s: float = 0.0

    def age(self, now: float) -> float:
        """How long ago this text was observed. Dispatch delay, not audio staleness."""
        return max(0.0, now - self.observed_at)

    def is_fresh(self, now: float, window_s: float = TRANSCRIPT_FRESHNESS_S) -> bool:
        """Fresh when neither the audio behind it nor its own dispatch has aged out."""
        return self.audio_age_s <= window_s and self.age(now) <= window_s


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    """A partial or settled transcript for one turn."""

    turn_id: int
    text: str
    is_final: bool
    stamp: FreshnessStamp
    #: ``voice`` or ``text``. Both are intent evidence, never authority (19.11).
    source: str = "voice"

    @property
    def generation(self) -> int:
        return self.stamp.generation


class TranscriptState:
    """Held hypothesis for one direction of speech, applying the 19.5 merge rule.

    THE SEAM AT A ROTATION
    ----------------------
    Rotation is make-before-break, so no audio frame is lost -- but the *replacement*
    connection never heard the audio that went to the previous one. Its first hypothesis
    therefore begins mid-utterance, and replacing the held text with it would silently
    drop the first half of a sentence the buyer did say.

    So a rotation calls :meth:`carry_over`, which moves whatever is held into a prefix.
    Within a generation the rule is unchanged and absolute -- replace, never append. Across
    the seam the prefix is joined to it once, because the two connections heard different
    halves of one utterance and neither is a revision of the other. The prefix is cleared
    when the turn settles.

    At a nine-minute rotation margin and a three-second utterance this fires for well under
    one turn in a hundred, which is exactly why it would otherwise never be noticed and
    would be blamed on the recognizer.
    """

    def __init__(self, *, clock: Clock, freshness_window_s: float = TRANSCRIPT_FRESHNESS_S):
        self._clock = clock
        self._window_s = freshness_window_s
        self._held = ""
        self._prefix = ""
        self._turn_id = 0
        self._last_final_text: str | None = None
        self._revised_since_final = False
        # Metrics (19.13)
        self.duplicates_dropped = 0
        self.stale_finals = 0

    @property
    def held(self) -> str:
        """The whole utterance so far: anything carried across a rotation, plus the
        current connection's hypothesis."""
        return self._joined(self._held)

    def _joined(self, text: str) -> str:
        if not self._prefix:
            return text
        return f"{self._prefix} {text}".strip() if text else self._prefix

    def carry_over(self) -> str:
        """Move the held hypothesis behind the seam. Called on rotation; returns the prefix.

        A no-op when nothing is held, which is the common case: most rotations land
        between utterances, and a prefix invented from silence would prepend stale words
        to the buyer's next sentence.
        """
        if self._held:
            self._prefix = self._joined(self._held)
            self._held = ""
        return self._prefix

    @property
    def turn_id(self) -> int:
        return self._turn_id

    def apply_interim(
        self, text: str | None, generation: int, audio_age_s: float = 0.0
    ) -> TranscriptTurn:
        """Replace the held hypothesis. Never presented as confirmed intent (19.5)."""
        self._held = apply_stream_text(self._held, text)
        self._revised_since_final = True
        return TranscriptTurn(
            turn_id=self._turn_id,
            text=self._joined(self._held),
            is_final=False,
            stamp=FreshnessStamp(
                observed_at=self._clock.now(), generation=generation, audio_age_s=audio_age_s
            ),
        )

    def apply_final(
        self, text: str | None, generation: int, audio_age_s: float = 0.0
    ) -> TranscriptTurn | None:
        """Settle the turn. Returns ``None`` for an empty turn or a duplicate final.

        Identical consecutive finals are deduplicated by content and turn: a final equal to
        the previous one with no revision in between is the recognizer repeating itself.
        """
        settled = self._joined(apply_stream_text(self._held, text))
        if not settled:
            return None
        if settled == self._last_final_text and not self._revised_since_final:
            self.duplicates_dropped += 1
            self._held = ""
            self._prefix = ""
            return None
        turn = TranscriptTurn(
            turn_id=self._turn_id,
            text=settled,
            is_final=True,
            stamp=FreshnessStamp(
                observed_at=self._clock.now(), generation=generation, audio_age_s=audio_age_s
            ),
        )
        self._last_final_text = settled
        self._revised_since_final = False
        self._turn_id += 1
        self._held = ""
        self._prefix = ""
        return turn

    def is_fresh(self, turn: TranscriptTurn) -> bool:
        """Whether this turn may still reach the agent (19.4 freshness applied to text)."""
        fresh = turn.stamp.is_fresh(self._clock.now(), self._window_s)
        if not fresh:
            self.stale_finals += 1
        return fresh
