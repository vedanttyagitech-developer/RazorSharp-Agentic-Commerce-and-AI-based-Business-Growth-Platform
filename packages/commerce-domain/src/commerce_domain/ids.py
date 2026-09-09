"""UUIDv7 identifiers: time-ordered, index-friendly, and not guessable in sequence."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as date_
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


class ReferenceFormatError(ValueError):
    """The text is not an order reference this platform could have emitted.

    Raised rather than returning ``None`` because every caller must decide what to do about
    it, and a ``None`` that flows onward becomes a lookup for nothing -- which is how a
    near-miss quietly turns into "no such order" instead of "that is not an order number".
    """


#: A reference as it is written: ``RS-260907-K7M4QX2``. Read case- and separator-insensitively
#: (see :func:`parse_order_reference`); emitted in exactly one shape by :func:`order_reference`.
_REFERENCE_PREFIX: Final[str] = "RS"
_REFERENCE_DATE_DIGITS: Final[int] = 6
_REFERENCE_TAIL_CHARS: Final[int] = 7


@dataclass(frozen=True, slots=True)
class ParsedOrderReference:
    """What can be read out of a reference, which is less than an order id and enough.

    ``date`` is the UTC day the order's id was minted, and it is the whole point: it turns
    "find this reference" from a scan into a comparison against one buyer's orders from one
    day. ``canonical`` is the reference as this platform writes it, so a caller can echo the
    buyer's number back to them in the form every other surface uses.
    """

    canonical: str
    date: date_
    tail: str


def parse_order_reference(text: str) -> ParsedOrderReference:
    """Read an order reference a person typed or said.

    **This does not invert :func:`order_reference` and cannot.** The tail is thirty-five
    bits drawn from the id's last sixty-four, so a reference names about one in thirty-four
    billion orders on a given day rather than exactly one. What comes back is the day and
    the tail; resolving those to an order is a lookup that compares candidates against
    ``order_reference`` itself, which keeps that function the only definition of what an
    order is called.

    Reading is deliberately more forgiving than writing. A buyer reads their number off a
    screen or a phone call and types it in whatever case, with or without the hyphens, with
    whatever spaces the paste brought along -- and all of those are the same order. What the
    platform *emits* stays exactly one shape; only what it accepts is generous.

    A near-miss raises rather than resolving. ``I``, ``L``, ``O`` and ``U`` are absent from
    the alphabet on purpose, so their presence is a typo rather than a character to map
    charitably: mapping ``O`` to ``0`` would silently hand back a different buyer's order.
    """
    squeezed = "".join(text.split()).replace("-", "").replace("_", "").upper()
    expected = len(_REFERENCE_PREFIX) + _REFERENCE_DATE_DIGITS + _REFERENCE_TAIL_CHARS
    if len(squeezed) != expected:
        raise ReferenceFormatError(f"an order reference has {expected} characters: {text!r}")
    if not squeezed.startswith(_REFERENCE_PREFIX):
        raise ReferenceFormatError(f"not this platform's order reference: {text!r}")
    digits = squeezed[len(_REFERENCE_PREFIX) : len(_REFERENCE_PREFIX) + _REFERENCE_DATE_DIGITS]
    tail = squeezed[len(_REFERENCE_PREFIX) + _REFERENCE_DATE_DIGITS :]
    if not digits.isdigit():
        raise ReferenceFormatError(f"the date in an order reference is six digits: {text!r}")
    if any(character not in _READABLE for character in tail):
        raise ReferenceFormatError(
            f"an order reference has no I, L, O or U -- those are typos, not characters "
            f"to guess at: {text!r}"
        )
    try:
        day = datetime.strptime(digits, "%y%m%d").replace(tzinfo=UTC).date()
    except ValueError as exc:
        raise ReferenceFormatError(f"not a date: {text!r}") from exc
    return ParsedOrderReference(
        canonical=f"{_REFERENCE_PREFIX}-{digits}-{tail}", date=day, tail=tail
    )
