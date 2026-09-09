"""An order reference a buyer says out loud has to be one the platform can look up.

THE DEFECT
----------
``order_reference`` renders ``orders.id`` as ``RS-260909-XW5G26M`` so a person can carry it
across a room, and every screen, every payload and the copilot's own speech use it. Nothing
could read one back. It is derived and never stored, no endpoint searched on it, and the
copilot's grounding lexicon matched only the raw UUID -- the one form nobody is ever shown.

So "Mera order RS-260909-XW5G26M kahan hai?" was unanswerable on every surface. Not a voice
gap: the identifier the whole product speaks in was write-only.

WHAT A PARSER CAN AND CANNOT DO
-------------------------------
It cannot invert the rendering, and that is not a flaw to be fixed later. The tail is 35
bits taken from the id's last 64, so a reference names roughly one in thirty-four billion
per day rather than exactly one order. What ``parse_order_reference`` gives is the *day*,
which turns a lookup into a comparison against one buyer's orders from one UTC date. The
comparison is against ``order_reference`` itself, so the reference stays the only definition
of what an order is called and there is no second implementation to drift.

Reading is deliberately more forgiving than writing. A buyer types what they can see, and a
lowercase reference, a stray space, or the hyphens left out is the same order -- whereas
what the platform *emits* stays exactly one shape.
"""

from __future__ import annotations

import uuid

import pytest
from commerce_domain import order_reference
from commerce_domain.ids import ReferenceFormatError, parse_order_reference, uuid7

_KNOWN = uuid.UUID("01a086ae-cde5-75bd-bbb4-bbd50c28179d")  # a real order, RS-260909-XW5G26M


def test_the_reference_this_platform_emits_reads_back() -> None:
    rendered = order_reference(_KNOWN)
    assert rendered == "RS-260909-XW5G26M"
    parsed = parse_order_reference(rendered)
    assert parsed.canonical == rendered
    assert (parsed.date.year, parsed.date.month, parsed.date.day) == (2026, 9, 9)


@pytest.mark.parametrize(
    "typed",
    [
        "rs-260909-xw5g26m",  # said to a keyboard, not copied
        "  RS-260909-XW5G26M  ",  # pasted with the whitespace around it
        "RS260909XW5G26M",  # hyphens left out
        "rs 260909 xw5g26m",  # read off a phone call
    ],
)
def test_a_buyer_types_what_they_can_see(typed: str) -> None:
    """Every one of these is the same order. Refusing them would be pedantry with a cost."""
    assert parse_order_reference(typed).canonical == "RS-260909-XW5G26M"


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "RS-260909",  # no tail
        "RS-260909-XW5G26",  # six tail characters, not seven
        "RS-260909-XW5G26MM",  # eight
        "XX-260909-XW5G26M",  # not this platform's prefix
        "RS-261399-XW5G26M",  # month 13
        "RS-260931-XW5G26M",  # September has thirty days
        "RS-260909-XW5G26I",  # I, L, O and U are not in the alphabet, on purpose
        "RS-260909-XW5G26L",
        "RS-260909-XW5G26O",
        "RS-260909-XW5G26U",
        "order 12345",
        "01a086ae-cde5-75bd-bbb4-bbd50c28179d",  # the id itself is not a reference
    ],
)
def test_what_is_not_a_reference_is_refused_rather_than_guessed(junk: str) -> None:
    """A near-miss must not resolve to somebody's order. Refusing is the safe direction."""
    with pytest.raises(ReferenceFormatError):
        parse_order_reference(junk)


def test_the_date_is_the_id_s_own_and_is_utc() -> None:
    """The window a resolver searches comes from here, so it must be the same clock.

    ``order_reference`` reads the UUIDv7 timestamp as UTC. A resolver that filtered on a
    local date would miss every order placed after 05:30 IST on the day before.
    """
    minted = uuid7(_now_ms=1757462400000)  # 2025-09-10T00:00:00Z
    parsed = parse_order_reference(order_reference(minted))
    assert (parsed.date.year, parsed.date.month, parsed.date.day) == (2025, 9, 10)


def test_round_tripping_holds_across_many_ids() -> None:
    """Not a sample of one: every reference this platform can emit must read back.

    Two orders on one day can render the same reference -- 35 bits of tail is a collision
    space, not an identity -- which is exactly why a resolver compares candidates rather
    than trusting a parse. What must never happen is a reference the parser rejects.
    """
    for millis in range(1_757_462_400_000, 1_757_462_400_000 + 500):
        rendered = order_reference(uuid7(_now_ms=millis))
        assert parse_order_reference(rendered).canonical == rendered


@pytest.mark.parametrize("before_the_epoch", ["RS-690101-ABCDEFG", "RS-691231-ABCDEFG"])
def test_a_date_this_platform_could_never_have_minted_is_refused(before_the_epoch: str) -> None:
    """``%y`` maps 69 to 1969, and 1969 is before the epoch.

    Found live as an HTTP 500 on ``GET /v1/orders?reference=``. The parser accepted the
    shape, the resolver turned the date into a UUIDv7 id bound, and a UUIDv7 timestamp is
    *unsigned* milliseconds since 1970 -- so a negative one raised ``OverflowError`` from
    ``int.to_bytes``, which is not a ``ReferenceFormatError`` and so escaped the router's
    handler entirely.

    Refused here rather than caught downstream, because it is the parser's own claim that is
    wrong: a reference is a rendering of a UUIDv7, and no UUIDv7 can carry a pre-epoch
    timestamp. A date this platform could not have minted is not a reference it emitted, and
    saying so gives the buyer the 422 they should have had instead of a 500.
    """
    with pytest.raises(ReferenceFormatError):
        parse_order_reference(before_the_epoch)


def test_the_epoch_boundary_itself_still_parses() -> None:
    """1970-01-01 is the first day a UUIDv7 can name, so it is valid and must stay valid.

    The RED direction for the test above: a fix that refused everything before, say, 2020
    would pass that test and quietly narrow what the platform can read back.
    """
    parsed = parse_order_reference("RS-700101-ABCDEFG")
    assert (parsed.date.year, parsed.date.month, parsed.date.day) == (1970, 1, 1)
