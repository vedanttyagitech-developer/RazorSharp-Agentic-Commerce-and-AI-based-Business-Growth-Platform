"""Gemini Transcribe Live over ``google-genai`` (19.2), via Application Default Credentials.

SDK surface, copied from the installed ``google-genai==2.22.0`` (``google/genai/live.py``
and ``types.py``), not recalled from documentation:

    genai.Client(vertexai=True, project=..., location="global")
    client.aio.live.connect(*, model: str, config: LiveConnectConfig) -> AsyncSession
        # connect() awaits the server's first message (setup_complete) before yielding
    AsyncSession.send_realtime_input(*, audio: Blob)          # mime "audio/pcm;rate=16000"
    AsyncSession.receive() -> AsyncIterator[LiveServerMessage]  # ends at turn_complete
    AsyncSession.close()
    LiveServerMessage.server_content.interim_input_transcription.text   # cumulative
    LiveServerMessage.server_content.input_transcription.text           # final
    LiveServerMessage.go_away.time_left, .session_resumption_update.new_handle/.resumable
    LiveServerMessage.voice_activity.voice_activity_type  # ACTIVITY_START / ACTIVITY_END

Three constraints that close the socket or 404 rather than validate (19.2):

- ``input_audio_transcription`` MUST be sent. Omitting it closes the socket with
  ``1007 Input audio transcription is required for ASR``. It looks like an empty optional
  field; it is mandatory. Do not optimise it away.
- The 3.5 transcribe models serve from ``global`` only. The location is pinned in
  ``constants.TRANSCRIBE_LOCATION`` and deliberately ignores ``GOOGLE_CLOUD_LOCATION``.
- The sample rate travels in the MIME string (``audio/pcm;rate=16000``), not in config.

``response_modalities`` is set to TEXT explicitly: the SDK substitutes ``["AUDIO"]`` when
it is absent, which is exactly the native-audio path this platform forbids (19.1).
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from ..constants import INPUT_MIME_TYPE, TRANSCRIBE_LOCATION, TRANSCRIBE_MODEL
from .events import (
    SttActivity,
    SttConnectionLostError,
    SttEvent,
    SttFinal,
    SttGoAway,
    SttInterim,
    SttResumption,
    SttSetupComplete,
)

if TYPE_CHECKING:
    from google import genai
    from google.genai.live import AsyncSession
    from google.genai.types import LiveServerMessage

log = logging.getLogger(__name__)

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s\s*$")


def _parse_time_left(value: str | None) -> float | None:
    if value is None:
        return None
    match = _DURATION.match(value)
    return float(match.group(1)) if match else None


def translate(message: LiveServerMessage) -> list[SttEvent]:
    """Map one ``LiveServerMessage`` to zero or more STT events. Pure; tested offline."""
    events: list[SttEvent] = []
    if message.setup_complete is not None:
        events.append(SttSetupComplete())
    content = message.server_content
    if content is not None:
        interim = content.interim_input_transcription
        if interim is not None and interim.text is not None:
            events.append(SttInterim(interim.text))
        final = content.input_transcription
        if final is not None and final.text is not None:
            events.append(SttFinal(final.text))
    if message.go_away is not None:
        events.append(SttGoAway(_parse_time_left(message.go_away.time_left)))
    update = message.session_resumption_update
    if update is not None:
        events.append(SttResumption(handle=update.new_handle, resumable=bool(update.resumable)))
    activity = message.voice_activity
    if activity is not None and activity.voice_activity_type is not None:
        kind = str(activity.voice_activity_type)
        if kind.endswith("ACTIVITY_START"):
            events.append(SttActivity(started=True))
        elif kind.endswith("ACTIVITY_END"):
            events.append(SttActivity(started=False))
    return events


def live_connect_config(
    *, resumption_handle: str | None, language_codes: Sequence[str] | None
) -> Any:
    """Build the ``LiveConnectConfig`` for a transcription-only session."""
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.TEXT],
        # MANDATORY (19.2): omitting this closes the socket with
        # "1007 Input audio transcription is required for ASR". Not optional.
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=list(language_codes) if language_codes else None
        ),
        session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
    )


class GeminiLiveSttSession:
    """``LiveSttSession`` over one ``AsyncSession``."""

    def __init__(
        self, context: AbstractAsyncContextManager[AsyncSession], session: AsyncSession
    ) -> None:
        self._context = context
        self._session = session
        self._closed = False

    async def send_audio(self, pcm: bytes) -> None:
        from google.genai import types

        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=INPUT_MIME_TYPE)
            )
        except Exception as exc:
            raise SttConnectionLostError(f"send failed: {exc}") from exc

    async def receive(self) -> AsyncIterator[SttEvent]:
        # The SDK's receive() returns after each turn_complete; re-enter until closed.
        while not self._closed:
            try:
                async for message in self._session.receive():
                    for event in translate(message):
                        yield event
            except Exception as exc:
                if self._closed:
                    return
                raise SttConnectionLostError(f"receive failed: {exc}") from exc

    async def close(self) -> None:
        self._closed = True
        try:
            await self._context.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover - network teardown
            log.warning("closing Transcribe Live session raised: %s", exc)


class GeminiTranscribeLiveFactory:
    """``LiveSttFactory`` for Gemini Transcribe Live. No network until ``open``."""

    def __init__(
        self,
        *,
        project: str,
        model: str = TRANSCRIBE_MODEL,
        language_codes: Sequence[str] | None = None,
        client: genai.Client | None = None,
    ) -> None:
        self._project = project
        self._model = model
        self._language_codes = tuple(language_codes) if language_codes else None
        self._client = client

    def _get_client(self) -> genai.Client:
        if self._client is None:
            from google import genai

            # Location pinned to "global" (19.2); GOOGLE_CLOUD_LOCATION is ignored on purpose.
            self._client = genai.Client(
                vertexai=True, project=self._project, location=TRANSCRIBE_LOCATION
            )
        return self._client

    async def open(self, *, resumption_handle: str | None) -> GeminiLiveSttSession:
        config = live_connect_config(
            resumption_handle=resumption_handle, language_codes=self._language_codes
        )
        context = self._get_client().aio.live.connect(model=self._model, config=config)
        try:
            # connect() yields only after the server's setup_complete arrived, which is
            # the make-before-break precondition (19.3).
            session = await context.__aenter__()
        except Exception as exc:
            raise SttConnectionLostError(f"connect failed: {exc}") from exc
        return GeminiLiveSttSession(context, session)
