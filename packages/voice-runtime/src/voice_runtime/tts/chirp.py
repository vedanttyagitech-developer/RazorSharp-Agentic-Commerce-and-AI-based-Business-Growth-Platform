"""Cloud Text-to-Speech, Chirp 3 HD voices (19.2), via Application Default Credentials.

SDK surface, copied from the installed ``google-cloud-texttospeech==2.37.0``:

    google.cloud.texttospeech.TextToSpeechAsyncClient().synthesize_speech(
        input=SynthesisInput(text=...),
        voice=VoiceSelectionParams(language_code=..., name=...),
        audio_config=AudioConfig(audio_encoding=AudioEncoding.LINEAR16,
                                 sample_rate_hertz=...),
    ) -> SynthesizeSpeechResponse   # .audio_content: bytes (WAV container)

The client is created lazily on first use so importing or constructing this class makes
no Google call; ``tests/test_voice_sdk_signatures.py`` fails on drift of these names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..wav import strip_wav_header
from .synth import VoiceSpec

if TYPE_CHECKING:
    from google.cloud.texttospeech import TextToSpeechAsyncClient


class ChirpSynthesizer:
    """``SpeechSynthesizer`` over Cloud TTS; returns raw PCM16 at ``voice.sample_rate_hz``."""

    def __init__(self, client: TextToSpeechAsyncClient | None = None) -> None:
        self._client = client

    def _get_client(self) -> TextToSpeechAsyncClient:
        if self._client is None:
            from google.cloud.texttospeech import TextToSpeechAsyncClient

            self._client = TextToSpeechAsyncClient()
        return self._client

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        from google.cloud import texttospeech

        response = await self._get_client().synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=str(voice.locale), name=voice.name
            ),
            # ``speaking_rate`` is honoured by Chirp 3 HD -- measured on this deployment,
            # where duration scales as almost exactly the inverse of the rate. It is worth
            # stating because it is not true of every field on this config: some Chirp 3 HD
            # voices ignore ``pitch``, so a prosody knob here is set only when it has been
            # shown to do something.
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=voice.sample_rate_hz,
                speaking_rate=voice.speaking_rate,
            ),
        )
        return strip_wav_header(bytes(response.audio_content))
