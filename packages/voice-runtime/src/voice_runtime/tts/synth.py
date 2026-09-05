"""Speech synthesis behind a Protocol, and the cancellable speaker (19.8).

``speak`` re-checks the generation after **every** await, not only before the first: a
sentence whose synthesis was already running when the buyer interrupted must still never
reach their ears. Barge-in detection itself is on the client (19.7); the server's part is
to bump the generation and honour it here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..constants import OUTPUT_SAMPLE_RATE_HZ, TRANSACTIONAL_VOICES
from .guard import GuardVerdict, SpeechGuard
from .templates import Locale

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceSpec:
    """Which voice to synthesise with; Chirp 3 HD for transactional speech (19.2)."""

    locale: Locale
    name: str
    sample_rate_hz: int = OUTPUT_SAMPLE_RATE_HZ


def voice_for(locale: Locale) -> VoiceSpec:
    return VoiceSpec(locale=locale, name=TRANSACTIONAL_VOICES[str(locale)])


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
    _seq: int = 0

    async def speak(
        self, text: str, *, locale: Locale, deterministic: bool, generation: int
    ) -> SpeakResult:
        """Speak ``text`` sentence by sentence while ``generation`` is still current."""
        verdict = self.guard.check(text, deterministic=deterministic)
        for refusal in verdict.refused:
            log.warning("guard refused model sentence (%s): %r", refusal.reason, refusal.sentence)
        voice = self.voice_for_locale(locale)
        sent = 0
        for sentence in verdict.allowed:
            if not self.generation.is_current(generation):
                return SpeakResult(sent, cancelled=True, tts_failed=False, refused=verdict)
            try:
                pcm = await self.synthesizer.synthesize(sentence, voice)
            except Exception as exc:
                log.exception("TTS synthesis failed; deterministic text stays visible")
                return SpeakResult(
                    sent, cancelled=False, tts_failed=True, refused=verdict, failure_detail=str(exc)
                )
            if not self.generation.is_current(generation):
                # Cancelled DURING synthesis: the check that gets forgotten (19.8).
                return SpeakResult(sent, cancelled=True, tts_failed=False, refused=verdict)
            self._seq += 1
            await self.sink.send_chunk(
                SpeechChunk(
                    seq=self._seq,
                    generation=generation,
                    text=sentence,
                    pcm=pcm,
                    sample_rate_hz=voice.sample_rate_hz,
                    deterministic=deterministic,
                )
            )
            sent += 1
            if not self.generation.is_current(generation):
                return SpeakResult(sent, cancelled=True, tts_failed=False, refused=verdict)
        return SpeakResult(sent, cancelled=False, tts_failed=False, refused=verdict)
