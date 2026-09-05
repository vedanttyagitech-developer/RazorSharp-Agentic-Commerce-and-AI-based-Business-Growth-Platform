"""A monotonic clock behind a Protocol so freshness and TTL properties are testable.

Every timestamp in this package is a monotonic float in seconds. Wall time is never used
for freshness decisions: a wall-clock step would either replay stale audio or expire live
tickets, and neither is acceptable in a payment conversation.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Monotonic seconds. Only ``now`` is required."""

    def now(self) -> float: ...


class MonotonicClock:
    """The production clock: ``time.monotonic``."""

    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    """A clock the test advances by hand, so freshness windows are exact, not flaky."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a monotonic clock never runs backwards")
        self._now += seconds
