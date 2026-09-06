"""UUIDv7 identifiers: time-ordered, index-friendly, and not guessable in sequence."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Final


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


#: Crockford's base32, which drops I, L, O and U.
#:
#: Chosen because this alphabet is meant to be read down a phone line and written on a
#: delivery slip: there is no one/ell and no zero/oh to get wrong, and no accidental words.
_READABLE: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def order_reference(order_id: uuid.UUID) -> str:
    """The order id, in a form a person can say out loud: ``RS-260907-K7M4QX2``.

    Why this exists. ``orders.id`` is a UUIDv7 and it is the identity -- the payment
    attempt, the policy receipt and every audit row point at it, and none of that changes.
    But ``01a0786d-30ce-7e03-a33e-351594c47565`` is not something a buyer can read back to
    a support agent, a merchant can quote in a message, or anyone can carry across a room.
    A shop that cannot name its own orders in words has an identifier and no reference.

    So this is a *rendering* of that id and not a second one. It is derived, never stored:
    the same id always produces the same reference, nothing can drift out of step with
    anything, and there is no counter to contend on and no new column to migrate. The date
    is the id's own UUIDv7 timestamp, which makes the reference mean something to the person
    holding it -- "my order from the 7th" -- and the tail is the id's random bits in an
    alphabet with no ambiguous characters.

    Seven tail characters, which is thirty-four billion per day. Two different orders on one
    day rendering the same reference is possible in the way a hash collision is possible; it
    is not a second identity and nothing is resolved by it alone, which is why the id stays
    the thing every table joins on.
    """
    raw = order_id.bytes
    stamp = int.from_bytes(raw[:6], "big")
    when = datetime.fromtimestamp(stamp / 1000, tz=UTC)
    tail = int.from_bytes(raw[8:], "big")
    letters = "".join(_READABLE[(tail >> (5 * position)) & 0x1F] for position in range(7))
    return f"RS-{when:%y%m%d}-{letters}"
