"""One lifecycle per user intent, independent of speech cancellation.

The ledger never cancels an in-flight API request: a timeout or a replaced answer
cannot establish whether a remote mutation occurred. Only delivery is suppressed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IntentOutcome(StrEnum):
    ANSWERED = "answered"
    EMPTY = "empty"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STALE = "stale"
    ROUTED_TO_CHECKOUT = "routed_to_checkout"
    DISCONNECTED = "disconnected"


@dataclass(slots=True)
class Intent:
    intent_id: int
    turn_id: int
    source: str
    started: bool = False
    outcome: IntentOutcome | None = None
    speech_suppressed: bool = False


class IntentLedger:
    """Session-local ordering and exactly-once completion; no buyer authority."""

    def __init__(self, history_limit: int = 128) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._limit = history_limit
        self._next = 0
        self.latest = 0
        self._entries: dict[int, Intent] = {}

    def open(self, turn_id: int, source: str, *, supersedes: bool = True) -> Intent:
        self._next += 1
        entry = Intent(self._next, turn_id, source)
        self._entries[entry.intent_id] = entry
        if supersedes:
            self.latest = entry.intent_id
        self._prune()
        return entry

    def _prune(self) -> None:
        closed = [key for key, entry in self._entries.items() if entry.outcome is not None]
        for key in closed[: -self._limit]:
            del self._entries[key]

    def get(self, intent_id: int) -> Intent | None:
        return self._entries.get(intent_id)

    def start(self, intent_id: int) -> bool:
        entry = self.get(intent_id)
        if entry is None or entry.outcome is not None or entry.started:
            return False
        entry.started = True
        return True

    def deliverable(self, intent_id: int) -> bool:
        entry = self.get(intent_id)
        return entry is not None and entry.outcome is None and intent_id == self.latest

    def close(self, intent_id: int, outcome: IntentOutcome) -> Intent | None:
        entry = self.get(intent_id)
        if entry is None or entry.outcome is not None:
            return None
        entry.outcome = outcome
        self._prune()
        return entry

    def interrupt_speech(self) -> None:
        for entry in self._entries.values():
            if entry.outcome is None:
                entry.speech_suppressed = True

    def pending(self) -> tuple[Intent, ...]:
        return tuple(entry for entry in self._entries.values() if entry.outcome is None)
