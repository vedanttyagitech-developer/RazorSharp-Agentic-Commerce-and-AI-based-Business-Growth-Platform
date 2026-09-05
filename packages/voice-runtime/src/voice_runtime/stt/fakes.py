"""Fake recognizer connections for tests: scriptable, offline, and audio-free.

The fake records every frame it was sent and lets a test emit events into the receive
loop, so rotation, stale-generation dropping and reconnect behaviour are asserted without
a microphone or a network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from .events import (
    LiveSttSession,
    SttConnectionLostError,
    SttEvent,
    SttFinal,
    SttInterim,
    SttSetupComplete,
)


class FakeSttSession:
    """One fake connection. Frames land in ``sent``; ``emit`` pushes events to ``receive``."""

    def __init__(
        self,
        index: int,
        *,
        resumption_handle: str | None,
        final_after_frames: int | None = None,
        scripted_text: str = "",
        fail_send: bool = False,
    ) -> None:
        self.index = index
        self.resumption_handle = resumption_handle
        self.sent: list[bytes] = []
        self.closed = False
        # ``None`` is the close sentinel; an exception instance is raised from ``receive``.
        self._events: asyncio.Queue[SttEvent | SttConnectionLostError | None] = asyncio.Queue()
        self._final_after_frames = final_after_frames
        self._scripted_text = scripted_text
        self._fail_send = fail_send
        self._scripted_done = False

    # ---- test controls ---------------------------------------------------------------

    def emit(self, event: SttEvent) -> None:
        self._events.put_nowait(event)

    def drop_connection(self) -> None:
        """Simulate an abnormal close: ``receive`` raises ``SttConnectionLostError``."""
        self._events.put_nowait(SttConnectionLostError("fake connection dropped"))

    # ---- LiveSttSession --------------------------------------------------------------

    async def send_audio(self, pcm: bytes) -> None:
        if self.closed or self._fail_send:
            raise SttConnectionLostError("fake socket closed")
        self.sent.append(pcm)
        if (
            self._final_after_frames is not None
            and not self._scripted_done
            and len(self.sent) >= self._final_after_frames
        ):
            # Scripted recognition: an interim, then the final, driven by audio arriving.
            self._scripted_done = True
            self.emit(SttInterim(self._scripted_text[: max(1, len(self._scripted_text) // 2)]))
            self.emit(SttFinal(self._scripted_text))

    async def receive(self) -> AsyncIterator[SttEvent]:
        yield SttSetupComplete()
        while True:
            item = await self._events.get()
            if item is None:
                return
            if isinstance(item, SttConnectionLostError):
                raise item
            yield item

    async def close(self) -> None:
        self.closed = True
        self._events.put_nowait(None)


class FakeSttFactory:
    """Creates ``FakeSttSession``s; keeps them in ``sessions`` for assertions."""

    def __init__(
        self,
        *,
        fail_opens: Sequence[bool] = (),
        final_after_frames: int | None = None,
        scripted_text: str = "",
        open_delay_s: float = 0.0,
    ) -> None:
        self.sessions: list[FakeSttSession] = []
        self._fail_opens = list(fail_opens)
        self._final_after_frames = final_after_frames
        self._scripted_text = scripted_text
        self._open_delay_s = open_delay_s
        self.open_calls = 0

    async def open(self, *, resumption_handle: str | None) -> LiveSttSession:
        self.open_calls += 1
        if self._open_delay_s:
            await asyncio.sleep(self._open_delay_s)
        if self._fail_opens and self._fail_opens.pop(0):
            raise SttConnectionLostError("fake open failed")
        session = FakeSttSession(
            len(self.sessions),
            resumption_handle=resumption_handle,
            final_after_frames=self._final_after_frames,
            scripted_text=self._scripted_text,
        )
        self.sessions.append(session)
        return session

    @property
    def current(self) -> FakeSttSession:
        return self.sessions[-1]
