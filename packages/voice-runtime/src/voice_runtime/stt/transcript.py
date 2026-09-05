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
    """Held hypothesis for one direction of speech, applying the 19.5 merge rule."""

    def __init__(self, *, clock: Clock, freshness_window_s: float = TRANSCRIPT_FRESHNESS_S):
        self._clock = clock
        self._window_s = freshness_window_s
        self._held = ""
        self._turn_id = 0
        self._last_final_text: str | None = None
        self._revised_since_final = False
        # Metrics (19.13)
        self.duplicates_dropped = 0
        self.stale_finals = 0

    @property
    def held(self) -> str:
        return self._held

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
            text=self._held,
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
        settled = apply_stream_text(self._held, text)
        if not settled:
            return None
        if settled == self._last_final_text and not self._revised_since_final:
            self.duplicates_dropped += 1
            self._held = ""
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
        return turn

    def is_fresh(self, turn: TranscriptTurn) -> bool:
        """Whether this turn may still reach the agent (19.4 freshness applied to text)."""
        fresh = turn.stamp.is_fresh(self._clock.now(), self._window_s)
        if not fresh:
            self.stale_finals += 1
        return fresh
