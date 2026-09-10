"""Provider-independent listening assembly and bounded microphone pre-roll.

Partial transcripts are revisions, never executable requests. A committed final
is emitted once, only after provider endpoint and a quiet period. Interpretation
of products, corrections, budgets and payment choices belongs to existing services.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HeardTurn:
    sequence: int
    text: str


class ListeningTurn:
    def __init__(self, quiet_ms: int = 550, max_chars: int = 4000) -> None:
        if quiet_ms < 0 or max_chars < 1:
            raise ValueError("Invalid listening limits")
        self.quiet_ms = quiet_ms
        self.max_chars = max_chars
        self.sequence = 0
        self.text = ""
        self.speaking = False
        self._quiet_since: float | None = None
        self._endpoint = False
        self._committed = True

    def activity(self, active: bool, now_ms: float) -> None:
        if active:
            if self._committed:
                self.sequence += 1
                self.text = ""
                self._committed = False
            self._endpoint = False
            self._quiet_since = None
        elif self.speaking:
            self._quiet_since = now_ms
        self.speaking = active

    def transcript(self, text: str, *, final: bool, sequence: int) -> bool:
        if sequence != self.sequence or self._committed:
            return False
        if len(text) > self.max_chars:
            raise ValueError("Transcript exceeds the turn limit")
        if text.strip():
            self.text = text.strip()
        self._endpoint = final and bool(self.text)
        return True

    def settle(self, now_ms: float) -> HeardTurn | None:
        if (
            self._committed
            or self.speaking
            or not self._endpoint
            or self._quiet_since is None
            or now_ms - self._quiet_since < self.quiet_ms
        ):
            return None
        self._committed = True
        return HeardTurn(self.sequence, self.text)


class OnsetBuffer:
    """Keeps PCM immediately before detected speech, bounded by bytes, not wall time."""

    def __init__(self, max_bytes: int = 6400) -> None:
        if max_bytes < 2 or max_bytes % 2:
            raise ValueError("PCM16 buffer must have a positive even byte capacity")
        self.max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def append(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            raise ValueError("Invalid PCM16")
        self._chunks.append(pcm[-self.max_bytes :])
        self._size += min(len(pcm), self.max_bytes)
        while self._size > self.max_bytes:
            chunk = self._chunks.popleft()
            excess = self._size - self.max_bytes
            if len(chunk) > excess:
                self._chunks.appendleft(chunk[excess:])
                self._size -= excess
            else:
                self._size -= len(chunk)

    def drain(self) -> bytes:
        result = b"".join(self._chunks)
        self._chunks.clear()
        self._size = 0
        return result
