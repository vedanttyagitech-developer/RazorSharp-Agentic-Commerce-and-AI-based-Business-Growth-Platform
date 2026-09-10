"""Provider-neutral synthesis fallback contract; no production provider is installed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from .synth import SpeechSynthesizer, VoiceSpec

log = logging.getLogger(__name__)


class FallbackSynthesizer:
    """Try each synthesiser in order; report which one spoke so it can be made visible.

    ``degraded_to`` names the synthesiser that actually spoke whenever it was not the
    first choice. The pipeline reads it and surfaces a degradation, because a voice that
    quietly changes is a change the buyer cannot account for (19.12).
    """

    def __init__(
        self,
        *synthesizers: SpeechSynthesizer,
        names: tuple[str, ...] = (),
        first_audio_timeout_s: float | None = None,
    ) -> None:
        if not synthesizers:
            raise ValueError("a fallback chain needs at least one synthesizer")
        self.first_audio_timeout_s = first_audio_timeout_s
        self._synthesizers = synthesizers
        self._names = names or tuple(type(s).__name__ for s in synthesizers)
        self.degraded_to: str | None = None
        #: Metrics (19.13): how often each link in the chain was the one that spoke.
        self.spoke: dict[str, int] = dict.fromkeys(self._names, 0)

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        failures: list[str] = []
        for index, synthesizer in enumerate(self._synthesizers):
            try:
                pcm = await synthesizer.synthesize(text, voice)
            except Exception as exc:
                failures.append(f"{self._names[index]}: {exc}")
                log.warning("TTS %s failed: %s", self._names[index], exc)
                continue
            self.spoke[self._names[index]] += 1
            self.degraded_to = self._names[index] if index else None
            return pcm
        raise RuntimeError("every synthesizer failed: " + "; ".join(failures))

    @property
    def supports_streaming(self) -> bool:
        return bool(getattr(self._synthesizers[0], "supports_streaming", False))

    async def stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        failures: list[str] = []
        for index, synthesizer in enumerate(self._synthesizers):
            sent = False
            try:
                stream = getattr(synthesizer, "stream", None)
                if callable(stream):
                    iterator = stream(text, voice)
                    try:
                        if index == 0 and self.first_audio_timeout_s is not None:
                            async with asyncio.timeout(self.first_audio_timeout_s):
                                pcm = await anext(iterator)
                                while not pcm:
                                    pcm = await anext(iterator)
                            sent = True
                            self.degraded_to = None
                            yield pcm
                        async for pcm in iterator:
                            if not pcm:
                                continue
                            sent = True
                            self.degraded_to = self._names[index] if index else None
                            yield pcm
                    finally:
                        await iterator.aclose()
                else:
                    pcm = await synthesizer.synthesize(text, voice)
                    sent = True
                    self.degraded_to = self._names[index] if index else None
                    yield pcm
                if not sent:
                    raise RuntimeError("TTS response carried no audio")
            except Exception as exc:
                # Never replay a partially spoken financial sentence through a fallback.
                if sent:
                    raise
                failures.append(f"{self._names[index]}: {exc}")
                continue
            self.spoke[self._names[index]] += 1
            return
        raise RuntimeError("every synthesizer failed: " + "; ".join(failures))

    def consume_degradation(self) -> str | None:
        """The fallback that spoke, once, so the caller surfaces it exactly one time."""
        degraded, self.degraded_to = self.degraded_to, None
        return degraded
