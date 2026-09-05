"""Conversational TTS on Vertex, and the fallback chain that keeps a voice available.

Specification 19.2 pins two synthesisers for two jobs:

* **Transactional** speech -- amounts, payment outcomes, refunds -- is Cloud TTS Chirp 3
  HD (:mod:`voice_runtime.tts.chirp`), for a steady voice on the sentences that matter.
* **Conversational** speech is ``gemini-3.1-flash-tts-preview`` with voice ``Kore``,
  falling back to ``gemini-2.5-flash-tts``.

Both were exercised against the installed ``google-genai==2.22.0`` on Vertex rather than
recalled from documentation. Two things measured there and not written anywhere else:

* The response carries **raw PCM, not a WAV container**: the mime type comes back as
  ``audio/l16; rate=24000; channels=1`` on the 3.1 model and
  ``audio/L16;codec=pcm;rate=24000`` on 2.5. Same bytes, different spelling, so the rate
  is read out of the mime string when it is there and only assumed when it is not.
  :func:`~voice_runtime.wav.strip_wav_header` still guards the path and returns
  non-RIFF input unchanged.
* These models serve from ``global``, like the transcribe models, and that is where the
  client is pinned.

:class:`FallbackSynthesizer` is the 19.12 rule in code: when the preferred synthesiser is
unavailable the next one speaks and the buyer is **told**, because a quieter, different
voice with no explanation is exactly the silent degradation the specification calls a
defect. When none can speak, the deterministic text is already on screen and stays there.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Final

from ..audio import resample_pcm16
from ..constants import OUTPUT_SAMPLE_RATE_HZ
from ..wav import strip_wav_header
from .synth import SpeechSynthesizer, VoiceSpec

if TYPE_CHECKING:
    from google import genai

__all__ = [
    "CONVERSATIONAL_MODEL",
    "CONVERSATIONAL_MODEL_FALLBACK",
    "CONVERSATIONAL_VOICE",
    "TTS_LOCATION",
    "FallbackSynthesizer",
    "GeminiSynthesizer",
    "rate_from_mime",
]

log = logging.getLogger(__name__)

#: Conversational TTS pin, and its fallback (19.2).
CONVERSATIONAL_MODEL: Final[str] = "gemini-3.1-flash-tts-preview"
CONVERSATIONAL_MODEL_FALLBACK: Final[str] = "gemini-2.5-flash-tts"
CONVERSATIONAL_VOICE: Final[str] = "Kore"
#: Like the transcribe models, these serve from ``global``; a regional endpoint is not
#: assumed to work and ``GOOGLE_CLOUD_LOCATION`` is deliberately ignored.
TTS_LOCATION: Final[str] = "global"

_RATE_IN_MIME: Final[re.Pattern[str]] = re.compile(r"rate=(\d+)", re.IGNORECASE)


def rate_from_mime(mime_type: str | None) -> int:
    """Sample rate out of ``audio/l16; rate=24000; channels=1``, or the output default.

    Both model pins state the rate in the mime string but spell the type differently, so
    the number is read rather than the string matched.
    """
    if mime_type:
        match = _RATE_IN_MIME.search(mime_type)
        if match:
            return int(match.group(1))
    return OUTPUT_SAMPLE_RATE_HZ


class GeminiSynthesizer:
    """``SpeechSynthesizer`` over Gemini TTS on Vertex. No network until ``synthesize``."""

    def __init__(
        self,
        *,
        project: str,
        model: str = CONVERSATIONAL_MODEL,
        voice_name: str = CONVERSATIONAL_VOICE,
        client: genai.Client | None = None,
    ) -> None:
        self._project = project
        self._model = model
        self._voice_name = voice_name
        self._client = client

    def _get_client(self) -> genai.Client:
        if self._client is None:
            from google import genai

            self._client = genai.Client(vertexai=True, project=self._project, location=TTS_LOCATION)
        return self._client

    def _config(self) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice_name)
                )
            ),
        )

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        """Speak ``text``. Only ``voice.sample_rate_hz`` is read from ``voice``.

        This model takes its voice from ``CONVERSATIONAL_VOICE`` and offers no rate
        control, so the locale's chosen Chirp voice and its measured ``speaking_rate`` are
        both ignored here: a fallback that spoke Hindi in the Hindi voice would be a
        different feature, not this one. It matters because it means a fallback sounds
        audibly unlike the voice it replaced -- which is precisely why
        :class:`FallbackSynthesizer` records ``degraded_to`` and the pipeline surfaces it.
        A voice that quietly changes pace and timbre mid-conversation is the silent
        degradation 19.12 calls a defect.
        """
        response = await self._get_client().aio.models.generate_content(
            model=self._model, contents=text, config=self._config()
        )
        blob = self._first_audio(response)
        pcm = strip_wav_header(bytes(blob.data or b""))
        source_hz = rate_from_mime(blob.mime_type)
        if source_hz != voice.sample_rate_hz:
            pcm = resample_pcm16(pcm, from_hz=source_hz, to_hz=voice.sample_rate_hz)
        return pcm

    @staticmethod
    def _first_audio(response: Any) -> Any:
        candidates = getattr(response, "candidates", None) or ()
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or ():
                blob = getattr(part, "inline_data", None)
                if blob is not None and blob.data:
                    return blob
        raise RuntimeError("TTS response carried no audio")


class FallbackSynthesizer:
    """Try each synthesiser in order; report which one spoke so it can be made visible.

    ``degraded_to`` names the synthesiser that actually spoke whenever it was not the
    first choice. The pipeline reads it and surfaces a degradation, because a voice that
    quietly changes is a change the buyer cannot account for (19.12).
    """

    def __init__(self, *synthesizers: SpeechSynthesizer, names: tuple[str, ...] = ()) -> None:
        if not synthesizers:
            raise ValueError("a fallback chain needs at least one synthesizer")
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

    def consume_degradation(self) -> str | None:
        """The fallback that spoke, once, so the caller surfaces it exactly one time."""
        degraded, self.degraded_to = self.degraded_to, None
        return degraded
