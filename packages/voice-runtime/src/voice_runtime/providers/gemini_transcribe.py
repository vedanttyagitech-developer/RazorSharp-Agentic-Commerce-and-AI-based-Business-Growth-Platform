"""Gemini 3.5 Transcribe Live on GCP: recognition only, never commerce authority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..stt.events import (
    SttActivity,
    SttConnectionLostError,
    SttEvent,
    SttFinal,
    SttGoAway,
    SttInterim,
    SttResumption,
    SttSetupComplete,
)

MODEL = "gemini-3.5-transcribe-live-preview"


class GeminiTranscribeFactory:
    def __init__(self, project: str) -> None:
        self.project = project

    async def open(self, *, resumption_handle: str | None) -> GeminiTranscribeSession:
        session = GeminiTranscribeSession(self.project)
        await session.start(resumption_handle)
        return session


class GeminiTranscribeSession:
    def __init__(self, project: str) -> None:
        self.project = project
        self._client: Any = None
        self._context: Any = None
        self._session: Any = None
        self._closed = False

    async def start(self, resumption_handle: str | None) -> None:
        from google import genai
        from google.genai import types

        self._client = genai.Client(vertexai=True, project=self.project, location="global")
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.TEXT],
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=["en-IN", "hi-IN"],
                mode=types.AudioTranscriptionConfigMode.VERBATIM,
                custom_vocabulary=["Razorpay", "Reserve Pay", "iPhone", "Amul", "Maggi"],
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    prefix_padding_ms=200, silence_duration_ms=550
                )
            ),
            session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
        )
        self._context = self._client.aio.live.connect(model=MODEL, config=config)
        try:
            self._session = await asyncio.wait_for(self._context.__aenter__(), timeout=10)
            # The SDK consumes the handshake internally. Never feed audio before its ACK.
            if self._session.setup_complete is None:
                raise SttConnectionLostError("Transcribe setup was not acknowledged")
        except BaseException:
            await self.close()
            raise

    async def send_audio(self, pcm: bytes) -> None:
        from google.genai import types

        if self._closed or self._session is None:
            raise SttConnectionLostError("Transcribe connection is closed")
        try:
            await asyncio.wait_for(
                self._session.send_realtime_input(
                    audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")
                ),
                timeout=2,
            )
        except Exception as exc:
            raise SttConnectionLostError("Transcribe audio transport failed") from exc

    @staticmethod
    def events(message: Any) -> list[SttEvent]:
        events: list[SttEvent] = []
        content = message.server_content
        if content is not None:
            partial = content.interim_input_transcription
            final = content.input_transcription
            if partial is not None and partial.text:
                events.append(SttInterim(partial.text))
            if final is not None and final.text:
                events.append(SttFinal(final.text))
        activity = message.voice_activity or message.voice_activity_detection_signal
        if activity is not None:
            signal = str(
                getattr(activity, "voice_activity_type", None)
                or getattr(activity, "vad_signal_type", "")
            )
            if signal in ("ACTIVITY_START", "SPEECH_ACTIVITY_BEGIN"):
                events.append(SttActivity(True))
            elif signal in ("ACTIVITY_END", "SPEECH_ACTIVITY_END"):
                events.append(SttActivity(False))
        if message.go_away is not None:
            events.append(SttGoAway(time_left_s=None))
        resume = message.session_resumption_update
        if resume is not None:
            events.append(SttResumption(handle=resume.new_handle, resumable=bool(resume.resumable)))
        return events

    async def receive(self) -> AsyncIterator[SttEvent]:
        yield SttSetupComplete()
        try:
            # receive() ends at a model turn boundary, not at session shutdown.
            while not self._closed:
                delivered = False
                async for message in self._session.receive():
                    delivered = True
                    if self._closed:
                        return
                    for event in self.events(message):
                        yield event
                if not delivered and not self._closed:
                    raise SttConnectionLostError("Transcribe stream ended without a turn")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                raise SttConnectionLostError("Transcribe connection lost") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._context is not None:
                await self._context.__aexit__(None, None, None)
        finally:
            if self._client is not None:
                await self._client.aio.aclose()
