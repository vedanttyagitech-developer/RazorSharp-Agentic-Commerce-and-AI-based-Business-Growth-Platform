"""Streaming GCP speech with explicit PCM contracts and bounded cancellation.

Cloud TTS speaks already-grounded text. It has no
commerce tools. SDK clients are lazy and close with their owning connection.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from ..tts.currency import spoken_currency
from ..tts.synth import VoiceSpec


class GcpSpeechSynthesizer:
    supports_streaming = True

    def __init__(
        self, *, endpoint: str = "texttospeech.googleapis.com", voice_name: str = "Aoede"
    ) -> None:
        self.endpoint = endpoint
        self.voice_name = voice_name
        self._client: Any = None

    async def stream(self, text: str, voice: VoiceSpec) -> AsyncGenerator[bytes]:
        from google.api_core.client_options import ClientOptions
        from google.cloud import texttospeech as tts

        if self._client is None:
            self._client = tts.TextToSpeechAsyncClient(
                client_options=ClientOptions(api_endpoint=self.endpoint)
            )
        client = self._client
        responses = None

        async def requests() -> AsyncIterator[Any]:
            yield tts.StreamingSynthesizeRequest(
                streaming_config=tts.StreamingSynthesizeConfig(
                    voice=tts.VoiceSelectionParams(
                        language_code=str(voice.locale),
                        name=f"{voice.locale}-Chirp3-HD-{self.voice_name}",
                    ),
                    streaming_audio_config=tts.StreamingAudioConfig(
                        audio_encoding=tts.AudioEncoding.PCM,
                        sample_rate_hertz=voice.sample_rate_hz,
                        speaking_rate=voice.speaking_rate,
                    ),
                )
            )
            yield tts.StreamingSynthesizeRequest(
                input=tts.StreamingSynthesisInput(text=spoken_currency(text, voice.locale))
            )

        try:
            responses = await client.streaming_synthesize(requests=requests(), timeout=20)
            remainder = b""
            async for response in responses:
                data = remainder + bytes(response.audio_content)
                aligned = len(data) - len(data) % 2
                if aligned:
                    yield data[:aligned]
                remainder = data[aligned:]
            if remainder:
                raise ValueError("Provider ended with an incomplete PCM16 sample")
        finally:
            if responses is not None and hasattr(responses, "cancel"):
                responses.cancel()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.transport.close()
            self._client = None

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        return b"".join([pcm async for pcm in self.stream(text, voice)])
