"""Bounded admission for a single-process service; never a distributed rate limiter.

All dimensions are checked atomically. Rejections do not consume another dimension's
budget. Idle entries expire; active leases are never evicted to admit fresh identities.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass


class WorkloadExceededError(Exception):
    """The caller should retry later; no expensive work has been started."""


@dataclass
class _Window:
    start: float
    used: int = 0
    active: int = 0


class WorkloadGate:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 4096):
        self.clock = clock
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}

    @contextmanager
    def admit(self, limits: Sequence[tuple[str, int, int]]) -> Iterator[None]:
        """Each dimension is (key, starts per minute, concurrent active limit)."""
        with self._lock:
            now = self.clock()
            self._windows = {
                key: value
                for key, value in self._windows.items()
                if value.active or now - value.start < 60
            }
            missing = {key for key, _, _ in limits} - self._windows.keys()
            if len(self._windows) + len(missing) > self.max_keys:
                raise WorkloadExceededError
            windows = []
            for key, rate, concurrent in limits:
                window = self._windows.get(key, _Window(now))
                if now - window.start >= 60:
                    window.start, window.used = now, 0
                if window.used >= rate or window.active >= concurrent:
                    raise WorkloadExceededError
                windows.append((key, window))
            for key, window in windows:
                window.used += 1
                window.active += 1
                self._windows[key] = window
        try:
            yield
        finally:
            with self._lock:
                for _, window in windows:
                    window.active -= 1
