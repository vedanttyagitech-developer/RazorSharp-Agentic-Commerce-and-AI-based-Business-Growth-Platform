"""UUIDv7 identifiers: time-ordered, index-friendly, and not guessable in sequence."""

from __future__ import annotations

import os
import uuid


def uuid7(*, _now_ms: int | None = None) -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562): 48-bit big-endian millisecond timestamp, then random.

    Time-ordered primary keys keep PostgreSQL B-tree inserts local, which matters for the
    high-write audit and outbox tables.
    """
    if _now_ms is None:
        import time

        _now_ms = int(time.time() * 1000)
    ts = _now_ms.to_bytes(6, "big")
    rand = bytearray(os.urandom(10))
    rand[0] = (rand[0] & 0x0F) | 0x70  # version 7
    rand[2] = (rand[2] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(ts) + bytes(rand))


def uuid7_str() -> str:
    return str(uuid7())
