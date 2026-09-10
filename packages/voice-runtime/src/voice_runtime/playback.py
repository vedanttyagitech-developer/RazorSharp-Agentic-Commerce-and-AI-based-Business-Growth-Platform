"""Utterance identity, separate from intent and provider generation.

Sending PCM does not mean that the buyer heard it. Actual playback-start and
playback-end acknowledgements establish that lifecycle, never payment approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass(slots=True)
class Utterance:
    utterance_id: int
    intent_id: int | None
    announced_at: float = 0.0
    started_at: float | None = None
    sent: bool = False
    terminal: str | None = None


class PlaybackLedger:
    def __init__(self, history_limit: int = 128) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._limit = history_limit
        self._sequence = 0
        self._entries: dict[int, Utterance] = {}

    def announce(self, intent_id: int | None, now: float | None = None) -> Utterance:
        self._sequence += 1
        utterance = Utterance(
            self._sequence, intent_id, announced_at=monotonic() if now is None else now
        )
        self._entries[utterance.utterance_id] = utterance
        return utterance

    def started(self, utterance_id: int, now: float) -> bool:
        entry = self._entries.get(utterance_id)
        if entry is None or entry.terminal is not None or entry.started_at is not None:
            return False
        entry.started_at = now
        return True

    def sent(self, utterance_id: int) -> bool:
        entry = self._entries.get(utterance_id)
        if entry is None or entry.terminal is not None:
            return False
        entry.sent = True
        return True

    def end(self, utterance_id: int, reason: str = "played") -> bool:
        entry = self._entries.get(utterance_id)
        if entry is None or entry.terminal is not None:
            return False
        if reason == "played" and (entry.started_at is None or not entry.sent):
            return False
        entry.terminal = reason
        closed = [key for key, row in self._entries.items() if row.terminal is not None]
        for key in closed[: -self._limit]:
            del self._entries[key]
        return True

    def interrupt(self) -> tuple[int, ...]:
        pending = tuple(key for key, row in self._entries.items() if row.terminal is None)
        for key in pending:
            self.end(key, "interrupted")
        return pending

    def expire(self, now: float, timeout_s: float = 300.0) -> tuple[int, ...]:
        expired = tuple(
            key
            for key, row in self._entries.items()
            if row.terminal is None and now - row.announced_at >= timeout_s
        )
        for key in expired:
            self.end(key, "ack_timeout")
        return expired

    @property
    def audible(self) -> frozenset[int]:
        return frozenset(
            key
            for key, row in self._entries.items()
            if row.started_at is not None and row.terminal is None
        )
