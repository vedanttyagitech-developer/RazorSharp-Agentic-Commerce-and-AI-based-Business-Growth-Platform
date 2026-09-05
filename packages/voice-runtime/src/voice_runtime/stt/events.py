"""The STT connection boundary: events a live recognizer emits and the Protocols behind it.

Everything the rotation logic needs from Google is expressed here as plain values, so the
19.14 properties (rotation without loss, stale-generation drop, queue eviction, echo
substitution) are exercised against a fake with no audio and no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


class SttConnectionLostError(RuntimeError):
    """The live connection closed abnormally. The session reconnects; the commerce
    session is untouched (19.12)."""


@dataclass(frozen=True, slots=True)
class SttSetupComplete:
    """The server acknowledged setup. Only after this may the frame writer switch (19.3)."""


@dataclass(frozen=True, slots=True)
class SttInterim:
    """``interim_input_transcription.text``: cumulative and revisable (19.5)."""

    text: str


@dataclass(frozen=True, slots=True)
class SttFinal:
    """``input_transcription.text``: the settled utterance, the turn (19.5)."""

    text: str


@dataclass(frozen=True, slots=True)
class SttActivity:
    """Server voice-activity detection: ``ACTIVITY_START`` / ``ACTIVITY_END``."""

    started: bool


@dataclass(frozen=True, slots=True)
class SttGoAway:
    """Server ``go_away``: an early rotation trigger, never an error (19.3)."""

    time_left_s: float | None


@dataclass(frozen=True, slots=True)
class SttResumption:
    """Session-resumption handle carried across a rotation instead of starting cold."""

    handle: str | None
    resumable: bool


SttEvent = SttSetupComplete | SttInterim | SttFinal | SttActivity | SttGoAway | SttResumption


class LiveSttSession(Protocol):
    """One open recognizer connection."""

    async def send_audio(self, pcm: bytes) -> None:
        """Send one PCM16 LE 16 kHz mono frame; ``SttConnectionLostError`` on a dead socket."""
        ...

    def receive(self) -> AsyncIterator[SttEvent]:
        """Yield events until the connection ends. Raises ``SttConnectionLostError`` on an
        abnormal close; returns normally after ``close()``."""
        ...

    async def close(self) -> None: ...


class LiveSttFactory(Protocol):
    """Opens connections. ``open`` returns only once the connection is receiving, which is
    what makes rotation make-before-break (19.3)."""

    async def open(self, *, resumption_handle: str | None) -> LiveSttSession: ...
