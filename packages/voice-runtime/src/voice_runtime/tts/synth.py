"""Speech synthesis behind a Protocol, and the cancellable speaker (19.8).

``speak`` re-checks the generation after **every** await, not only before the first: a
sentence whose synthesis was already running when the buyer interrupted must still never
reach their ears. Barge-in detection itself is on the client (19.7); the server's part is
to bump the generation and honour it here.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..constants import (
    MAX_SYNTHESIS_CHARS,
    OUTPUT_SAMPLE_RATE_HZ,
    SPEAKING_RATE,
    SYNTHESIS_LOOKAHEAD,
    TRANSACTIONAL_VOICES,
)
from .guard import GuardVerdict, SpeechGuard
from .templates import Locale
from .tokenizer import split_for_synthesis

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceSpec:
    """Which voice to synthesise with; Chirp 3 HD for transactional speech (19.2).

    ``speaking_rate`` travels with the voice rather than being read from constants at the
    call site, because it is a property OF the voice: two voices reading the same sentence
    at the same nominal rate do not take the same time, so a rate that is not attached to
    the voice it was measured against is a number with nothing behind it.
    """

    locale: Locale
    name: str
    sample_rate_hz: int = OUTPUT_SAMPLE_RATE_HZ
    #: Multiplier on the voice's own pace. A synthesiser that cannot vary rate ignores it.
    speaking_rate: float = SPEAKING_RATE


def voice_for(locale: Locale) -> VoiceSpec:
    return VoiceSpec(
        locale=locale, name=TRANSACTIONAL_VOICES[str(locale)], speaking_rate=SPEAKING_RATE
    )


class SpeechSynthesizer(Protocol):
    """Synthesise one sentence to raw PCM16 LE mono at ``voice.sample_rate_hz``."""

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SpeechChunk:
    """One sentence of audio, with the generation that produced it."""

    seq: int
    generation: int
    text: str
    pcm: bytes
    sample_rate_hz: int
    deterministic: bool


class SpeechSink(Protocol):
    async def send_chunk(self, chunk: SpeechChunk) -> None: ...


class SpeechGeneration:
    """The cancel token: a monotonically increasing generation (19.8)."""

    def __init__(self) -> None:
        self._current = 0

    @property
    def current(self) -> int:
        return self._current

    def bump(self) -> int:
        """Cancel everything in flight. Returns the new generation."""
        self._current += 1
        return self._current

    def is_current(self, generation: int) -> bool:
        return generation == self._current


@dataclass(frozen=True, slots=True)
class SpeakResult:
    chunks_sent: int
    cancelled: bool
    tts_failed: bool
    refused: GuardVerdict
    failure_detail: str | None = None


class FakeSynthesizer:
    """Deterministic offline synthesizer: ``len(text)`` samples of a marker byte.

    ``hold`` lets a test park synthesis mid-flight so the cancelled-DURING-synthesis
    property is observable; ``fail`` raises so TTS-failure degradation is testable.
    """

    def __init__(self, *, fail: bool = False, bytes_per_char: int = 2) -> None:
        self.calls: list[tuple[str, VoiceSpec]] = []
        self.fail = fail
        self.hold: asyncio.Event | None = None
        self._bytes_per_char = bytes_per_char

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        self.calls.append((text, voice))
        if self.hold is not None:
            await self.hold.wait()
        if self.fail:
            raise RuntimeError("fake synthesizer failure")
        return b"\x01\x00" * (len(text) * self._bytes_per_char // 2)


@dataclass
class Speaker:
    """Turns text into guarded, chunked, cancellable speech."""

    synthesizer: SpeechSynthesizer
    sink: SpeechSink
    generation: SpeechGeneration
    guard: SpeechGuard = field(default_factory=SpeechGuard)
    voice_for_locale: Callable[[Locale], VoiceSpec] = voice_for
    #: Longest phrase sent to the synthesiser in one call. Latency, not content: nothing
    #: is dropped, it simply starts playing sooner.
    max_synthesis_chars: int = MAX_SYNTHESIS_CHARS
    #: How many phrases may be synthesised ahead of the one being sent. Two is enough to
    #: hide synthesis behind playback without holding much unsent audio in memory, and a
    #: barge-in cancels the look-ahead along with everything else.
    lookahead: int = SYNTHESIS_LOOKAHEAD
    _seq: int = 0

    async def speak(
        self,
        text: str,
        *,
        locale: Locale,
        deterministic: bool,
        generation: int,
        grounded_amounts_minor: frozenset[int] = frozenset(),
        identifiers_allowed: bool = False,
    ) -> SpeakResult:
        """Speak ``text`` phrase by phrase while ``generation`` is still current.

        Synthesis runs a little ahead of sending, because it is far slower than playback:
        on this deployment a phrase takes seconds to synthesise and seconds to play, and
        doing them strictly one after the other leaves the buyer in silence for the sum
        rather than the larger of the two. Chunks are still SENT in order -- the client
        schedules playback from a single advancing play head -- and the generation is
        re-checked after every await, so a barge-in cancels work that is already running.
        """
        verdict = self.guard.check(
            text,
            deterministic=deterministic,
            grounded_amounts_minor=grounded_amounts_minor,
            identifiers_allowed=identifiers_allowed,
        )
        for refusal in verdict.refused:
            log.warning("guard refused model sentence (%s): %r", refusal.reason, refusal.sentence)
        voice = self.voice_for_locale(locale)
        # The GUARD's unit is the sentence; the SYNTHESISER's may be smaller. Splitting an
        # already-approved sentence into phrases only ever shortens what is spoken in one
        # call, never what was checked, so 19.9's one-tokenizer rule is preserved: a
        # phrase reaching the synthesiser was part of a sentence approved entire.
        phrases = [
            phrase
            for sentence in verdict.allowed
            for phrase in split_for_synthesis(sentence, self.max_synthesis_chars)
        ]
        if not phrases:
            return SpeakResult(0, cancelled=False, tts_failed=False, refused=verdict)

        pending: deque[asyncio.Task[bytes]] = deque()
        next_index = 0

        def launch() -> None:
            nonlocal next_index
            pending.append(
                asyncio.create_task(self.synthesizer.synthesize(phrases[next_index], voice))
            )
            next_index += 1

        def abandon() -> None:
            """Drop the look-ahead. Runs on every exit path, including exceptions.

            A task that already finished has its exception retrieved rather than dropped:
            an un-retrieved task exception is reported at garbage-collection time, from
            somewhere unrelated, which is the least useful place to learn that TTS failed.
            """
            while pending:
                task = pending.popleft()
                if task.done():
                    if not task.cancelled():
                        task.exception()
                else:
                    task.cancel()

        spoken = 0
        # At least one, always: a look-ahead of zero would launch nothing, fall straight
        # through the loop and report success having said nothing at all.
        ahead = max(1, self.lookahead)
        while next_index < min(ahead, len(phrases)):
            launch()
        try:
            while pending:
                if not self.generation.is_current(generation):
                    return SpeakResult(spoken, cancelled=True, tts_failed=False, refused=verdict)
                task = pending.popleft()
                try:
                    pcm = await task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("TTS synthesis failed; deterministic text stays visible")
                    return SpeakResult(
                        spoken,
                        cancelled=False,
                        tts_failed=True,
                        refused=verdict,
                        failure_detail=str(exc),
                    )
                if not self.generation.is_current(generation):
                    # Cancelled DURING synthesis: the check that gets forgotten (19.8).
                    return SpeakResult(spoken, cancelled=True, tts_failed=False, refused=verdict)
                if next_index < len(phrases):
                    launch()
                self._seq += 1
                await self.sink.send_chunk(
                    SpeechChunk(
                        seq=self._seq,
                        generation=generation,
                        text=phrases[spoken],
                        pcm=pcm,
                        sample_rate_hz=voice.sample_rate_hz,
                        deterministic=deterministic,
                    )
                )
                spoken += 1
                if not self.generation.is_current(generation):
                    return SpeakResult(spoken, cancelled=True, tts_failed=False, refused=verdict)
        finally:
            abandon()
        return SpeakResult(spoken, cancelled=False, tts_failed=False, refused=verdict)
