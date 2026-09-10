"""One routing decision per grounded reply; exact transactional speech stays on Chirp."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Protocol

from ..tts.fallback import FallbackSynthesizer
from ..tts.synth import VoiceSpec
from .gcp import GcpSpeechSynthesizer
from .gemini_voice import GeminiLiveVoice

# Conservative routing, not a grounding validator. Speaker validates the text first.
# Ambiguous/short replies and numeric/action facts always use literal synthesis.
EXACT = re.compile(
    r"\d|[₹$€£%]|\b(price|cost|rupees?|paid|payment|refund|reserve|balance|"
    r"limit|checkout|order|cart|basket|added|removed|quantity|stock|available|"
    r"discount|offer|approved|declined|failed|pending|confirmed|add|remove|"
    r"rupaye|rupay|paise|daam|jod|hata|bhugtan)\b|"
    r"रुप|कीमत|भुगतान|रिफंड|कार्ट|जोड़|हटा|मात्रा|ऑर्डर|छूट|सीमा|बकाया",
    re.IGNORECASE,
)
ADVICE = re.compile(
    r"\b(compare|comparison|compared|recommend|recommendation|prefer|"
    r"because|whereas|versus|tradeoff|consider|better|advice|"
    r"kyunki|behtar|tulna|salah)\b|तुलना|सलाह|बेहतर|क्योंकि",
    re.IGNORECASE,
)


def route_reply(text: str, voice: VoiceSpec) -> str:
    if voice.exact_wording or EXACT.search(text):
        return "chirp"
    # Length alone never selects native audio; an advice cue is required too.
    if len(text.split()) >= 35 and ADVICE.search(text):
        return "gemini"
    return "chirp"


class ManagedSpeech(Protocol):
    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes: ...
    def stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]: ...
    async def warmup(self) -> None: ...
    async def aclose(self) -> None: ...


class HybridSpeechSynthesizer:
    supports_streaming = True

    def __init__(
        self,
        project: str,
        *,
        chirp: ManagedSpeech | None = None,
        gemini: ManagedSpeech | None = None,
    ) -> None:
        self.chirp = chirp if chirp is not None else GcpSpeechSynthesizer(voice_name="Aoede")
        self.gemini = gemini if gemini is not None else GeminiLiveVoice(project, voice_name="Aoede")
        self.advice = FallbackSynthesizer(
            self.gemini, self.chirp, names=("gemini", "chirp"), first_audio_timeout_s=2.5
        )
        self.last_route: str | None = None

    async def warmup(self) -> None:
        # A failed optional warmup must not disable the literal speech path.
        try:
            await self.gemini.warmup()
        except Exception:
            logging.getLogger(__name__).warning("Optional voice warmup failed")

    async def stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        self.last_route = route_reply(text, voice)
        provider = self.advice if self.last_route == "gemini" else self.chirp
        iterator = provider.stream(text, voice)
        try:
            async for chunk in iterator:
                yield chunk
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        return b"".join([chunk async for chunk in self.stream(text, voice)])

    def consume_degradation(self) -> str | None:
        return self.advice.consume_degradation()

    async def aclose(self) -> None:
        await asyncio.gather(self.chirp.aclose(), self.gemini.aclose())
