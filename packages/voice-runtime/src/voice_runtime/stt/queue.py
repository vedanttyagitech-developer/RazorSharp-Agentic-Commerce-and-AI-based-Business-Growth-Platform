"""The audio queue: bounded by freshness, evicting the oldest, never drained (19.4).

Three load-bearing properties, each with a test in ``tests/test_voice_queue.py``:

1. The queue survives reconnects. There is deliberately no ``clear`` method: audio that
   arrives mid-reconnect is still here afterwards, provided it is inside the freshness
   window. Ageing a stale frame out is not the same act as draining.
2. Eviction is from the front. Dropping the newest frame discards the speech the buyer is
   producing right now; dropping the oldest discards audio that is already stale.
3. A bound exists at all. A microphone producing faster than the socket drains would
   otherwise grow memory without limit.

``feed`` never blocks and never raises into the caller.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass

from ..clock import Clock
from ..constants import MAX_FRESH_AUDIO_AGE_S, QUEUE_MAX_FRAMES

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QueuedFrame:
    """A microphone frame with the monotonic instant it was queued."""

    pcm: bytes
    enqueued_at: float


class FreshAudioQueue:
    """Bounded FIFO of microphone frames with oldest-first eviction and freshness ageing."""

    def __init__(
        self,
        *,
        clock: Clock,
        max_frames: int = QUEUE_MAX_FRAMES,
        max_age_s: float = MAX_FRESH_AUDIO_AGE_S,
    ) -> None:
        if max_frames < 1:
            raise ValueError("the queue must hold at least one frame")
        if max_age_s <= 0:
            raise ValueError("the freshness window must be positive")
        self._clock = clock
        self._max_frames = max_frames
        self._max_age_s = max_age_s
        self._frames: deque[QueuedFrame] = deque()
        self._available = asyncio.Event()
        # Metrics (19.13): counters that are never surfaced are not observability.
        self.frames_in = 0
        self.frames_out = 0
        self.evicted_overflow = 0
        self.evicted_stale = 0

    # ---- producer side ---------------------------------------------------------------

    def feed(self, pcm: bytes) -> None:
        """Enqueue a frame. Never blocks, never raises into the microphone path (19.4)."""
        try:
            while len(self._frames) >= self._max_frames:
                self._frames.popleft()  # evict the OLDEST, never the newest
                self.evicted_overflow += 1
            self._frames.append(QueuedFrame(pcm, self._clock.now()))
            self.frames_in += 1
            self._available.set()
        except Exception:  # pragma: no cover - defensive: the mic path must not fail
            log.exception("audio queue feed failed; frame dropped")

    # ---- consumer side ---------------------------------------------------------------

    def _age_out(self) -> None:
        """Discard frames older than the freshness bound. Counted and surfaced."""
        now = self._clock.now()
        while self._frames and now - self._frames[0].enqueued_at > self._max_age_s:
            self._frames.popleft()
            self.evicted_stale += 1

    def pop_fresh(self) -> QueuedFrame | None:
        """Return the oldest fresh frame, or ``None`` if nothing fresh is queued."""
        self._age_out()
        if not self._frames:
            self._available.clear()
            return None
        frame = self._frames.popleft()
        self.frames_out += 1
        if not self._frames:
            self._available.clear()
        return frame

    def push_front(self, frame: QueuedFrame) -> None:
        """Return a popped frame to the head, keeping its original timestamp.

        Used when a send fails after the pop so a rotation or reconnect loses no audio; the
        frame still ages out normally if the gap becomes prolonged.
        """
        self._frames.appendleft(frame)
        self.frames_out -= 1
        self._available.set()

    async def wait_fresh(self) -> None:
        """Block until a fresh frame is queued or ``wake`` is called. Does not pop.

        Separated from ``pop_fresh`` so a consumer can re-check its generation between the
        wait and the pop (19.8: re-check after every await) without losing a frame. May
        return spuriously after ``wake``; the caller re-checks and calls ``pop_fresh``.
        """
        self._age_out()
        if self._frames:
            return
        self._available.clear()
        await self._available.wait()

    def wake(self) -> None:
        """Release every waiter so it re-checks its generation (used on rotation)."""
        self._available.set()

    # ---- introspection ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def depth(self) -> int:
        return len(self._frames)

    @property
    def max_frames(self) -> int:
        return self._max_frames

    @property
    def max_age_s(self) -> float:
        return self._max_age_s
