"""Adversarial provenance: attempts to make a write name an id the session never saw.

``test_ar_provenance`` proves the gates work on the cases they were designed for. This
suite is the other half: it assumes an attacker who controls the model's tool arguments
and, in the state-poisoning section, the persisted session blob, and tries to walk an
unseen identifier past :mod:`agent_runtime.core.provenance` into a backend write.

Four claims are under attack.

*The silent-pass invariant.* On ADK an empty dict returned from a ``before_tool_callback``
means "no opinion, run the tool after all". A refusal that serialised to ``{}`` would
therefore not be a refusal at all -- it would be an approval that read like a denial in
the audit. Every gate, at every reason key, is checked here for a non-empty **and truthy**
``to_result()``, because ``bool({})`` is the actual test ADK applies.

*Normalisation asymmetry.* ``check_sku_provenance`` upper-cases; every other family
compares raw. Both directions are probed. Case folding is a deliberate widening (the model
may echo a SKU in either case); the absence of stripping and of Unicode normalisation is
the safe direction, and this suite pins it so a later "helpful" ``.strip()`` cannot be
added without a failing test to argue with.

*Fail-closed on corruption.* ``from_state`` promises that anything malformed yields an
*empty* record, so a corrupted blob holds every write rather than admitting an unknown id.
The malformed shapes here are chosen to be ones a hostile party would reach for.

*Eviction is not a bypass.* The cap makes a hostile loop of reads able to forget an id.
Forgetting causes a hold, so the cap is a usability cost and never a security one, and the
tests say so by name.

Tests marked ``xfail(strict=True)`` are the genuine holes found: they assert the behaviour
the module documents but does not yet have, and will fail loudly once the fix lands.

Pure and offline: every record here is built in-process, no backend and no model.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from agent_runtime.backends.base import (
    ApprovalCard,
    CartQuote,
    CheckoutStatus,
    PricedLine,
    Provenance,
)
from agent_runtime.core import (
    MAX_BASKET_LINES,
    MAX_LINE_QUANTITY,
    PROVENANCE_CAP,
    Held,
    SessionProvenance,
    check_checkout_provenance,
    check_line_count,
    check_order_provenance,
    check_quantity,
    check_sku_provenance,
    session_write_lock,
)
from commerce_domain import Money

from .conftest import MILK_SKU

#: A SKU with the exact shape the fixture catalogue issues, that no tool ever returns
#: here. "Well formed" is the point: shape is not provenance.
FOREIGN_SKU = "TATA-SALTX-777"
CHECKOUT_ID = "chk-adversarial-1"
CONTENT_HASH = "0f3c9a1b2d4e5f60"


def _quote() -> CartQuote:
    return CartQuote(
        lines=(
            PricedLine(
                MILK_SKU, "milk", 1, Money(2800, "INR"), Money(2800, "INR"), 0, Money.zero("INR")
            ),
        ),
        items_subtotal=Money(2800, "INR"),
        items_tax=Money.zero("INR"),
        delivery_fee=Money(2000, "INR"),
        delivery_tax=Money.zero("INR"),
        total=Money(4800, "INR"),
        free_delivery_applied=False,
        gap_to_free_delivery=Money(10000, "INR"),
        currency="INR",
        content_hash=CONTENT_HASH,
        provenance=Provenance("sim", 1, datetime.now(UTC)),
    )


def _approval(version: int = 1, content_hash: str = CONTENT_HASH) -> ApprovalCard:
    """One checkout version exactly as a tool result would carry it."""
    return ApprovalCard(
        CHECKOUT_ID, version, content_hash, CheckoutStatus.PENDING_APPROVAL, _quote()
    )


def _grounded() -> SessionProvenance:
    """A record holding one of every family, reached only through the public remember API."""
    record = SessionProvenance()
    record.remember_sku(
        MILK_SKU, unit_price_minor=2800, currency="INR", catalogue_revision=3, is_available=True
    )
    record.remember_approval(_approval())
    record.remember_order_id("ord-seen-1")
    return record


# ------------------------------------------------------- 1. the ADK silent-pass invariant
#
# The single most important property in the module: ADK reads a falsy callback return as
# "run the tool". Each entry below is one reachable refusal, named by the reason key the
# model is supposed to be able to branch on.

_REFUSALS: list[tuple[str, str, Any]] = [
    ("sku_not_returned", "provenance", check_sku_provenance(SessionProvenance(), FOREIGN_SKU)),
    (
        "checkout_not_returned",
        "provenance",
        check_checkout_provenance(SessionProvenance(), CHECKOUT_ID, 1, CONTENT_HASH),
    ),
    (
        "checkout_version_not_returned",
        "provenance",
        check_checkout_provenance(_grounded(), CHECKOUT_ID, 9, CONTENT_HASH),
    ),
    (
        "content_hash_mismatch",
        "provenance",
        check_checkout_provenance(_grounded(), CHECKOUT_ID, 1, "not-the-hash"),
    ),
    ("order_not_returned", "provenance", check_order_provenance(SessionProvenance(), "ord-x")),
    ("invalid_quantity", "quantity", check_quantity(True)),
    ("invalid_quantity", "quantity", check_quantity(-1)),
    ("quantity_exceeds_cap", "quantity", check_quantity(MAX_LINE_QUANTITY + 1)),
    (
        "basket_full",
        "line_count",
        check_line_count([f"GRO-FULL-{i:03d}" for i in range(MAX_BASKET_LINES)], FOREIGN_SKU, 1),
    ),
]


@pytest.mark.parametrize(
    ("reason_key", "gate", "held"), _REFUSALS, ids=[f"{r[0]}-{i}" for i, r in enumerate(_REFUSALS)]
)
def test_every_refusal_serialises_to_a_non_empty_truthy_dict(
    reason_key: str, gate: str, held: Held | None
) -> None:
    """A hold that serialised to ``{}`` would be a silent pass, not a hold.

    ``bool(result)`` is asserted directly because that -- not ``result == {}`` -- is the
    test the ADK callback path applies to a callback's return value.
    """
    assert held is not None, f"{reason_key} did not hold at all"
    result = held.to_result()
    assert bool(result) is True
    assert result != {}
    assert result["ok"] is False
    assert result["blocked"] == gate
    assert result["reason_key"] == reason_key
    # The instruction has to name a tool, or the model has no move that would succeed.
    assert isinstance(result["instruction"], str) and result["instruction"].strip()
    assert len(result) >= 4


def test_a_hold_carrying_no_detail_at_all_is_still_non_empty() -> None:
    """The envelope alone must carry the refusal; detail is enrichment, not the payload."""
    result = Held("provenance", "sku_not_returned", "call product first").to_result()
    assert bool(result) is True
    assert result == {
        "ok": False,
        "blocked": "provenance",
        "reason_key": "sku_not_returned",
        "instruction": "call product first",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [("ok", True), ("blocked", "nothing"), ("reason_key", "fine"), ("instruction", "")],
)
def test_a_detail_key_cannot_overwrite_the_refusal_envelope(key: str, value: object) -> None:
    envelope = {
        "ok": False,
        "blocked": "provenance",
        "reason_key": "sku_not_returned",
        "instruction": "call product first",
    }
    result = Held("provenance", "sku_not_returned", "call product first", {key: value}).to_result()
    assert result[key] == envelope[key]


# --------------------------------------------------------------- 2. ids nobody returned


@pytest.mark.parametrize(
    ("check", "identifier", "reason_key"),
    [
        pytest.param(
            lambda r, i: check_sku_provenance(r, i),
            FOREIGN_SKU,
            "sku_not_returned",
            id="sku",
        ),
        pytest.param(
            lambda r, i: check_checkout_provenance(r, i, 1, CONTENT_HASH),
            "chk-never-seen",
            "checkout_not_returned",
            id="checkout",
        ),
        pytest.param(
            lambda r, i: check_order_provenance(r, i),
            "ord-never-seen",
            "order_not_returned",
            id="order",
        ),
    ],
)
def test_an_identifier_no_tool_returned_is_held_in_every_family(
    check: Any, identifier: str, reason_key: str
) -> None:
    """A record grounded in *other* ids must not vouch for this one: provenance is per id."""
    held = check(_grounded(), identifier)
    assert held is not None and held.reason_key == reason_key
    assert bool(held.to_result()) is True


@pytest.mark.parametrize(
    ("check", "identifier"),
    [
        (lambda r, i: check_sku_provenance(r, i), FOREIGN_SKU),
        (lambda r, i: check_order_provenance(r, i), "ord-never-seen"),
    ],
)
def test_a_refusal_does_not_ground_the_id_it_refused(check: Any, identifier: str) -> None:
    """Otherwise a model could launder an id by calling the gate twice: refuse, then pass."""
    record = _grounded()
    before = (len(record.skus), len(record.orders))
    assert check(record, identifier) is not None
    assert check(record, identifier) is not None  # still refused on the retry
    assert (len(record.skus), len(record.orders)) == before


def test_a_well_formed_sku_from_another_tenant_is_held() -> None:
    """Shape is not provenance. The attack is a *legitimate* id the session never saw.

    ``FOREIGN_SKU`` has the same four-and-five-and-three shape the fixture catalogue
    issues, so nothing about validating the string could catch it -- only the record can.
    """
    record = _grounded()
    assert record.knows_sku(MILK_SKU)
    held = check_sku_provenance(record, FOREIGN_SKU)
    assert held is not None and held.reason_key == "sku_not_returned"
    assert held.to_result()["sku"] == FOREIGN_SKU


def test_an_empty_identifier_is_held_in_every_family() -> None:
    """An empty string is the degenerate unseen id; it must not read as a wildcard."""
    record = _grounded()
    assert check_sku_provenance(record, "") is not None
    assert check_order_provenance(record, "") is not None
    assert check_checkout_provenance(record, "", 1, CONTENT_HASH) is not None


# ------------------------------------------------- 3. case, whitespace and look-alikes

#: Spellings of a SKU the session *has* seen, each altered so it is a different string.
#: Every one must hold: ``check_sku_provenance`` upper-cases and does nothing else.
_HOSTILE_SKU_SPELLINGS = [
    pytest.param(f" {MILK_SKU} ", id="surrounding-spaces"),
    pytest.param(f"{MILK_SKU} ", id="trailing-space"),
    pytest.param(f" {MILK_SKU}", id="leading-space"),
    pytest.param(f"{MILK_SKU}\n", id="trailing-newline"),
    pytest.param(f"{MILK_SKU}\t", id="trailing-tab"),
    pytest.param(f"{MILK_SKU}​", id="trailing-zero-width-space"),
    pytest.param("AMUL-DAIRY​-001", id="interior-zero-width-space"),
    pytest.param("AMUL­-DAIRY-001", id="soft-hyphen"),
    pytest.param("АMUL-DAIRY-001", id="cyrillic-A-lookalike"),
    pytest.param("AMUL‐DAIRY-001", id="unicode-hyphen-lookalike"),
    pytest.param("AMUL-DAİRY-001", id="turkish-dotted-I-lookalike"),
    pytest.param("AMUL-DAIRY-００１", id="fullwidth-digits"),
    pytest.param(f"{MILK_SKU}\x00", id="trailing-nul"),
    pytest.param(f"{MILK_SKU}‮", id="right-to-left-override"),
]


@pytest.mark.parametrize("spelling", _HOSTILE_SKU_SPELLINGS)
def test_a_sku_that_is_not_byte_for_byte_the_one_seen_is_held(spelling: str) -> None:
    """Held is correct here even where it looks unhelpful, and is the direction to keep.

    A homoglyph or a zero-width joiner is a different string, so it is a different SKU as
    far as the record is concerned, and the write is refused. Stripping or normalising
    would make the gate guess which id the model meant; guessing is precisely what
    provenance exists to stop. This test exists so a later convenience ``.strip()`` has to
    argue with a failing assertion.
    """
    record = _grounded()
    held = check_sku_provenance(record, spelling)
    assert held is not None and held.reason_key == "sku_not_returned"
    assert bool(held.to_result()) is True


def test_the_whitespace_hold_is_surprising_but_fail_closed_not_a_hole() -> None:
    """``" X "`` holds even though ``X`` would pass: the gate upper-cases, never strips.

    Named explicitly because it is the one asymmetry a reviewer is likely to "fix". The
    cost is a model that padded its argument gets one hold and must retry; the benefit is
    that no normalisation step can ever be induced to map an attacker's string onto a
    legitimate id.
    """
    record = _grounded()
    assert check_sku_provenance(record, MILK_SKU) is None
    assert check_sku_provenance(record, f" {MILK_SKU} ") is not None
    assert check_sku_provenance(record, f" {MILK_SKU} ".strip()) is None


def test_sku_case_folding_is_symmetric_and_therefore_not_a_widening() -> None:
    """Both remember and check upper-case, so folding cannot admit an unseen id.

    A lower-case tool result and an upper-case write meet at the same key, and so do the
    reverse. The set of admitted strings grows only by case variants of ids the session
    genuinely saw -- never by a new id.
    """
    record = SessionProvenance()
    record.remember_sku(
        MILK_SKU.lower(), unit_price_minor=2800, currency="INR", catalogue_revision=3
    )
    assert check_sku_provenance(record, MILK_SKU) is None
    assert check_sku_provenance(record, MILK_SKU.lower()) is None
    assert check_sku_provenance(record, MILK_SKU.title()) is None
    assert check_sku_provenance(record, FOREIGN_SKU) is not None  # folding admits nothing new
    assert record.seen_skus() == frozenset({MILK_SKU})  # stored upper-cased either way


def test_upper_casing_collapses_two_ids_that_differ_only_in_case() -> None:
    """The one real consequence of folding, pinned so it is a decision and not a surprise.

    ``str.upper()`` is not injective: it length-changes on some scripts. Two catalogue ids
    that differ only by case -- or by a fold like ``ss``/``ß`` -- become one record entry,
    so seeing either grounds both. Every SKU the merchant issues is ASCII upper-case, so
    this is unreachable in practice; it would stop being unreachable the day case-sensitive
    or non-ASCII SKUs are issued.
    """
    record = SessionProvenance()
    record.remember_sku("straße-01", unit_price_minor=1, currency="INR", catalogue_revision=1)
    assert record.seen_skus() == frozenset({"STRASSE-01"})
    # A genuinely different catalogue id now passes the gate because of the fold.
    assert check_sku_provenance(record, "STRASSE-01") is None


@pytest.mark.parametrize(
    ("check", "seen", "variant"),
    [
        (lambda r, i: check_order_provenance(r, i), "ord-seen-1", "ORD-SEEN-1"),
        (lambda r, i: check_order_provenance(r, i), "ord-seen-1", " ord-seen-1 "),
        (
            lambda r, i: check_checkout_provenance(r, i, 1, CONTENT_HASH),
            CHECKOUT_ID,
            CHECKOUT_ID.upper(),
        ),
        (
            lambda r, i: check_checkout_provenance(r, i, 1, CONTENT_HASH),
            CHECKOUT_ID,
            f" {CHECKOUT_ID}",
        ),
    ],
)
def test_non_sku_families_compare_raw_so_any_variant_is_held(
    check: Any, seen: str, variant: str
) -> None:
    """The asymmetry with SKUs runs in the safe direction: stricter, never looser.

    Order, cart and checkout ids are opaque server-issued strings that a model
    copies rather than retypes, so exact comparison costs nothing and closes the whole
    normalisation attack surface for three of the four families.
    """
    record = _grounded()
    assert check(record, seen) is None
    held = check(record, variant)
    assert held is not None
    assert bool(held.to_result()) is True


# -------------------------------------------------------- 4. the cart_lines hatch


def test_the_basket_lines_hatch_admits_only_a_line_the_basket_actually_holds() -> None:
    """The intended case: a returning buyer removes a line from a cart that predates the session.

    In production the iterable is fed from ``backend.basket_get`` inside the session write
    lock (``capabilities/tools.py`` ``basket_set_line``), so its contents are the
    merchant's own record of the cart. Nothing the model writes reaches this parameter.
    """
    record = SessionProvenance()  # nothing read yet this session
    assert check_sku_provenance(record, FOREIGN_SKU, cart_lines=(FOREIGN_SKU,)) is None
    assert check_sku_provenance(record, FOREIGN_SKU.lower(), cart_lines=(FOREIGN_SKU,)) is None
    assert check_sku_provenance(record, MILK_SKU, cart_lines=(FOREIGN_SKU,)) is not None


@pytest.mark.parametrize(
    "cart_lines",
    [
        pytest.param((), id="empty"),
        pytest.param((MILK_SKU,), id="a-different-line"),
        pytest.param((f" {FOREIGN_SKU} ",), id="padded-line"),
        pytest.param(("АATA-SALTX-777",), id="homoglyph-line"),
        pytest.param(("*",), id="glob-that-is-not-a-wildcard"),
        pytest.param(("",), id="empty-line"),
        pytest.param((FOREIGN_SKU[:-1],), id="prefix-of-the-target"),
        pytest.param((f"{FOREIGN_SKU}X",), id="superstring-of-the-target"),
    ],
)
def test_the_hatch_does_not_widen_into_a_general_bypass(cart_lines: tuple[str, ...]) -> None:
    """Only an exact (case-folded) member of the cart passes; there is no pattern matching."""
    held = check_sku_provenance(SessionProvenance(), FOREIGN_SKU, cart_lines=cart_lines)
    assert held is not None and held.reason_key == "sku_not_returned"


def test_the_hatch_is_per_call_and_never_writes_the_sku_into_the_record() -> None:
    """A pass through the hatch must not ground the SKU for the *next* call.

    If it did, one write against a pre-existing line would licence every later write of
    that SKU, including after the buyer emptied the cart.
    """
    record = SessionProvenance()
    assert check_sku_provenance(record, FOREIGN_SKU, cart_lines=(FOREIGN_SKU,)) is None
    assert record.skus == {}
    assert not record.knows_sku(FOREIGN_SKU)
    assert check_sku_provenance(record, FOREIGN_SKU) is not None  # cart now empty: held again


def test_a_bare_string_passed_as_basket_lines_matches_nothing() -> None:
    """A caller mistake (``cart_lines="SKU"`` rather than ``("SKU",)``) fails closed.

    A string is iterable, so the ``any(...)`` walks characters. A real SKU is longer than
    one character and so can never equal one, which is the direction a bug should fall.
    """
    held = check_sku_provenance(SessionProvenance(), FOREIGN_SKU, cart_lines=FOREIGN_SKU)
    assert held is not None and held.reason_key == "sku_not_returned"


# ------------------------------------------------- 5. checkout version and content hash


def test_the_three_checkout_refusals_are_distinct_and_ordered() -> None:
    """Unknown checkout, unseen version and wrong hash are three different mistakes.

    The order matters: an unknown checkout must not leak whether some version exists, so
    the id check comes first and the version detail only appears once the id is known.
    """
    empty = SessionProvenance()
    unknown = check_checkout_provenance(empty, CHECKOUT_ID, 1, CONTENT_HASH)
    assert unknown is not None and unknown.reason_key == "checkout_not_returned"
    assert "version" not in unknown.to_result()

    record = _grounded()
    assert check_checkout_provenance(record, CHECKOUT_ID, 1, CONTENT_HASH) is None
    bad_version = check_checkout_provenance(record, CHECKOUT_ID, 2, CONTENT_HASH)
    assert bad_version is not None and bad_version.reason_key == "checkout_version_not_returned"
    bad_hash = check_checkout_provenance(record, CHECKOUT_ID, 1, "deadbeefdeadbeef")
    assert bad_hash is not None and bad_hash.reason_key == "content_hash_mismatch"


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(-(2**63), id="very-negative"),
        pytest.param(2, id="next-version-not-yet-shown"),
        pytest.param(10**9, id="huge"),
    ],
)
def test_a_version_the_session_was_not_shown_is_held(version: int) -> None:
    """Version 2 is the interesting one: it is the version that will exist, but does not yet.

    A model that guesses the bump rather than re-reading is exactly the failure the
    version/hash pair exists to catch.
    """
    held = check_checkout_provenance(_grounded(), CHECKOUT_ID, version, CONTENT_HASH)
    assert held is not None and held.reason_key == "checkout_version_not_returned"
    assert held.to_result()["version"] == version


@pytest.mark.parametrize(
    "content_hash",
    [
        pytest.param(CONTENT_HASH.upper(), id="upper-case"),
        pytest.param(CONTENT_HASH.replace("0", "O"), id="zero-for-letter-o"),
        pytest.param(f" {CONTENT_HASH} ", id="surrounding-space"),
        pytest.param(f"{CONTENT_HASH}\n", id="trailing-newline"),
        pytest.param("", id="empty"),
        pytest.param("null", id="the-string-null"),
        pytest.param(CONTENT_HASH[:-1], id="truncated"),
        pytest.param(f"{CONTENT_HASH}0", id="extended"),
    ],
)
def test_a_hash_that_is_not_exactly_the_one_shown_is_held(content_hash: str) -> None:
    """The hash is copied from a tool result, so byte equality is the whole contract."""
    held = check_checkout_provenance(_grounded(), CHECKOUT_ID, 1, content_hash)
    assert held is not None and held.reason_key == "content_hash_mismatch"
    assert bool(held.to_result()) is True


@pytest.mark.parametrize("content_hash", [None, 0, False, [], {}])
def test_a_hash_that_is_not_a_string_at_all_is_held(content_hash: object) -> None:
    """``SeenCheckout.knows`` compares with ``==``, so a non-string can only fail to match."""
    held = check_checkout_provenance(_grounded(), CHECKOUT_ID, 1, content_hash)  # type: ignore[arg-type]
    assert held is not None and held.reason_key == "content_hash_mismatch"


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(True, id="bool-true"),
        pytest.param(1.0, id="float"),
        pytest.param(Decimal(1), id="decimal"),
    ],
)
def test_the_checkout_gate_itself_refuses_a_version_that_is_not_an_int(version: object) -> None:
    held = check_checkout_provenance(_grounded(), CHECKOUT_ID, version, CONTENT_HASH)  # type: ignore[arg-type]
    assert held is not None


# ----------------------------------------------------------- 6. quantity and line count


class _DuckInt:
    """A quantity that converts to an int but is not one -- a numpy scalar in miniature."""

    def __int__(self) -> int:
        return 1

    def __index__(self) -> int:
        return 1


@pytest.mark.parametrize(
    ("quantity", "reason_key"),
    [
        pytest.param(True, "invalid_quantity", id="bool-true"),
        pytest.param(False, "invalid_quantity", id="bool-false"),
        pytest.param(1.0, "invalid_quantity", id="float-whole"),
        pytest.param(0.0, "invalid_quantity", id="float-zero"),
        pytest.param(Decimal(1), "invalid_quantity", id="decimal"),
        pytest.param(_DuckInt(), "invalid_quantity", id="duck-typed-int"),
        pytest.param("1", "invalid_quantity", id="numeric-string"),
        pytest.param(None, "invalid_quantity", id="none"),
        pytest.param([1], "invalid_quantity", id="list"),
        pytest.param(-1, "invalid_quantity", id="negative"),
        pytest.param(-(10**30), "invalid_quantity", id="hugely-negative"),
        pytest.param(MAX_LINE_QUANTITY + 1, "quantity_exceeds_cap", id="one-over-cap"),
        pytest.param(10**100, "quantity_exceeds_cap", id="astronomically-over-cap"),
    ],
)
def test_a_quantity_that_is_not_a_whole_number_in_range_is_held(
    quantity: object, reason_key: str
) -> None:
    """``bool`` first, because ``True`` is an ``int`` and a model that wrote ``true`` meant a flag.

    ``False`` is held too even though it equals the legal value 0: a boolean where a count
    belongs is a malformed call, and silently reading it as "remove the line" would be the
    gate guessing.
    """
    held = check_quantity(quantity)  # type: ignore[arg-type]
    assert held is not None and held.reason_key == reason_key
    assert held.to_result()["blocked"] == "quantity"
    assert bool(held.to_result()) is True


@pytest.mark.parametrize("quantity", [0, 1, MAX_LINE_QUANTITY - 1, MAX_LINE_QUANTITY])
def test_a_whole_number_up_to_and_including_the_cap_passes(quantity: int) -> None:
    """Exactly the cap is legal; the refusal starts one above it."""
    assert check_quantity(quantity) is None


def test_an_int_subclass_is_a_whole_number_and_passes() -> None:
    """``IntEnum`` and friends are genuinely integers; only ``bool`` is the special case."""

    class Units(int):
        pass

    assert check_quantity(Units(3)) is None


def test_the_line_count_cap_holds_a_new_line_but_never_a_change_or_a_removal() -> None:
    """A cart at the cap must still be *editable*, or the buyer is stuck holding it."""
    full = [f"GRO-FULL-{i:03d}" for i in range(MAX_BASKET_LINES)]
    held = check_line_count(full, FOREIGN_SKU, 1)
    assert held is not None and held.reason_key == "basket_full"
    assert held.to_result()["line_count"] == MAX_BASKET_LINES
    assert held.to_result()["cap"] == MAX_BASKET_LINES

    assert check_line_count(full, full[0], MAX_LINE_QUANTITY) is None  # change an existing line
    assert check_line_count(full, full[0].lower(), 5) is None  # ... in either case
    assert check_line_count(full, FOREIGN_SKU, 0) is None  # a removal never fills a cart
    assert check_line_count(full[:-1], FOREIGN_SKU, 1) is None  # one under the cap
    # Already over the cap (state moved under us): still editable, still no new lines.
    over = [*full, "GRO-EXTRA-001"]
    assert check_line_count(over, over[0], 2) is None
    assert check_line_count(over, FOREIGN_SKU, 1) is not None


def test_case_folded_duplicate_lines_collapse_when_counted() -> None:
    """Two spellings of one SKU are one line, which is the merchant's own view of the cart.

    Worth pinning because the count is taken over a *set* of upper-cased ids: a caller that
    handed in duplicates cannot inflate the count and cannot deflate it either.
    """
    full = [f"GRO-FULL-{i:03d}" for i in range(MAX_BASKET_LINES)]
    with_dupe = [*full, full[0].lower()]
    assert check_line_count(with_dupe, FOREIGN_SKU, 1) is not None  # still full, not full+1


# --------------------------------------------- 7. state round-trip and blob poisoning
#
# ``from_state`` is handed whatever sits under ``acr:provenance`` in ADK session state.
# The contract is that anything malformed produces an EMPTY record: every write then holds
# until a fresh read re-grounds it. Empty is safe; permissive is not.


def _sku_entry(**overrides: Any) -> dict[str, Any]:
    """One well-formed ``skus`` entry, with named fields replaced by hostile values."""
    entry: dict[str, Any] = {
        "sku": "X",
        "unit_price_minor": 1,
        "currency": "INR",
        "catalogue_revision": 1,
    }
    entry.update(overrides)
    return entry


_HOSTILE_BLOBS: list[Any] = [
    pytest.param(None, id="none"),
    pytest.param("junk", id="string"),
    pytest.param(b"junk", id="bytes"),
    pytest.param(42, id="int"),
    pytest.param([], id="list-not-dict"),
    pytest.param([{"skus": []}], id="list-of-dicts"),
    pytest.param({"skus": "AMUL-DAIRY-001"}, id="skus-as-a-bare-string"),
    pytest.param({"skus": {"AMUL-DAIRY-001": {}}}, id="skus-as-a-mapping"),
    pytest.param({"skus": ["AMUL-DAIRY-001"]}, id="skus-as-a-list-of-strings"),
    pytest.param({"skus": [None]}, id="sku-entry-is-none"),
    pytest.param({"skus": [{"sku": "X"}]}, id="sku-entry-missing-price"),
    pytest.param({"skus": [_sku_entry(unit_price_minor="free")]}, id="price-is-not-a-number"),
    pytest.param({"skus": [_sku_entry(unit_price_minor=None)]}, id="price-is-none"),
    pytest.param({"skus": [_sku_entry(unit_price_minor=[1])]}, id="price-is-a-list"),
    pytest.param({"skus": [_sku_entry(catalogue_revision="r")]}, id="revision-is-not-a-number"),
    pytest.param(
        {"checkouts": [{"checkout_id": "c", "versions": ["1", "h"]}]}, id="versions-a-list"
    ),
    pytest.param({"checkouts": [{"checkout_id": "c", "versions": None}]}, id="versions-none"),
    pytest.param({"checkouts": [{"checkout_id": "c"}]}, id="checkout-missing-versions"),
    pytest.param({"checkouts": [{"versions": {"1": "h"}}]}, id="checkout-missing-id"),
    pytest.param(
        {"checkouts": [{"checkout_id": "c", "versions": {"latest": "h"}}]}, id="version-key-not-int"
    ),
    pytest.param({"carts": "b1"}, id="carts-a-bare-string"),
    pytest.param({"orders": {"o1": None}}, id="orders-a-mapping"),
    pytest.param({"skus": [[[[[{"sku": "X"}]]]]]}, id="deeply-nested-junk"),
]


@pytest.mark.parametrize("blob", _HOSTILE_BLOBS)
def test_a_malformed_state_blob_yields_a_completely_empty_record(blob: object) -> None:
    """Every family must be empty, not just the one that was malformed.

    ``from_state`` builds into a local record and returns a *fresh* one on failure, so a
    blob that is well formed up to the poisoned entry cannot leave its earlier entries
    behind. That is the property being checked, not merely that no exception escaped.
    """
    record = SessionProvenance.from_state(blob)
    assert record.skus == {}
    assert record.carts == {}
    assert record.checkouts == {}
    assert record.orders == {}
    # And therefore every write is held.
    assert check_sku_provenance(record, MILK_SKU) is not None
    assert check_order_provenance(record, "ord-seen-1") is not None


def test_a_blob_poisoned_after_a_valid_prefix_discards_the_prefix_too() -> None:
    """The attacker's best shape: real ids first, junk last, hoping for a partial record."""
    blob = {
        "skus": [
            _sku_entry(sku=MILK_SKU, unit_price_minor=2800),
            _sku_entry(sku=FOREIGN_SKU, unit_price_minor="gratis"),
        ],
        "orders": ["ord-seen-1"],
    }
    record = SessionProvenance.from_state(blob)
    assert record.skus == {} and record.orders == {}
    assert check_sku_provenance(record, MILK_SKU) is not None


def test_a_well_formed_blob_is_trusted_because_only_the_platform_writes_it() -> None:
    """Stated so the trust boundary is explicit rather than implied.

    ``from_state`` validates *shape*, not authenticity: a well-formed blob naming any id
    produces a record that vouches for it. The blob is written only by ``_save`` from tool
    results, so it inherits the session's trust. Anyone who can forge session state has
    already won a bigger prize than a cart line, and this gate is not the boundary that
    stops them -- the session store's isolation is.
    """
    blob = {"orders": ["ord-forged"]}
    record = SessionProvenance.from_state(blob)
    assert check_order_provenance(record, "ord-forged") is None


def test_a_round_trip_preserves_every_family_and_its_age_order() -> None:
    """Order is age, and eviction is oldest-first, so a reordering round trip would evict wrong."""
    record = SessionProvenance()
    for i in range(5):
        record.remember_sku(
            f"GRO-AGED-{i:03d}", unit_price_minor=100 + i, currency="INR", catalogue_revision=1
        )
        record.remember_order_id(f"ord-{i}")
    record.remember_approval(_approval(1))
    record.remember_approval(_approval(2, "second-hash"))

    restored = SessionProvenance.from_state(json.loads(json.dumps(record.to_state())))
    assert list(restored.skus) == list(record.skus)
    assert list(restored.orders) == list(record.orders)
    assert restored.skus[f"GRO-AGED-{2:03d}"].unit_price_minor == 102
    # Both checkout versions survive with their own hashes.
    assert check_checkout_provenance(restored, CHECKOUT_ID, 1, CONTENT_HASH) is None
    assert check_checkout_provenance(restored, CHECKOUT_ID, 2, "second-hash") is None
    assert check_checkout_provenance(restored, CHECKOUT_ID, 2, CONTENT_HASH) is not None


def test_the_record_crosses_a_turn_boundary_because_it_lives_in_session_state() -> None:
    """Per session, not per turn: ids are stable facts, so re-reading them is not required.

    A turn boundary is exactly a ``to_state``/``from_state`` pair (``capabilities/tools.py``
    ``_save``/``_load`` against ADK session state), so this is the whole mechanism. Money
    facts do *not* cross: those live in the per-turn grounding ledger, which is why the
    price stored beside a SKU here is never evidence for a sentence.
    """
    turn_one = _grounded()
    turn_two = SessionProvenance.from_state(json.loads(json.dumps(turn_one.to_state())))
    assert check_sku_provenance(turn_two, MILK_SKU) is None
    assert check_order_provenance(turn_two, "ord-seen-1") is None
    assert check_checkout_provenance(turn_two, CHECKOUT_ID, 1, CONTENT_HASH) is None
    # The price rode along so a hold can name it -- it is a record, not evidence.
    assert turn_two.skus[MILK_SKU].unit_price_minor == 2800


def test_an_id_from_a_previous_turn_that_the_blob_never_carried_is_held() -> None:
    """A new conversation is a new record: last week's SKU is not this session's provenance."""
    fresh = SessionProvenance.from_state({})
    assert check_sku_provenance(fresh, MILK_SKU) is not None
    assert check_order_provenance(fresh, "ord-seen-1") is not None
    assert check_checkout_provenance(fresh, CHECKOUT_ID, 1, CONTENT_HASH) is not None


def test_a_blob_longer_than_the_cap_keeps_only_the_newest_entries() -> None:
    """The cap is enforced on the way *in* as well, so an oversized blob cannot bloat memory."""
    blob = {
        "skus": [
            {
                "sku": f"GRO-BIG-{i:04d}",
                "unit_price_minor": i,
                "currency": "INR",
                "catalogue_revision": 1,
            }
            for i in range(PROVENANCE_CAP + 25)
        ],
        "orders": [f"ord-{i}" for i in range(PROVENANCE_CAP + 25)],
    }
    record = SessionProvenance.from_state(blob)
    assert len(record.skus) == PROVENANCE_CAP
    assert len(record.orders) == PROVENANCE_CAP
    assert not record.knows_sku("GRO-BIG-0000")  # oldest dropped
    assert record.knows_sku(f"GRO-BIG-{PROVENANCE_CAP + 24:04d}")  # newest kept


@pytest.mark.parametrize("field_name", ["unit_price_minor", "catalogue_revision"])
@pytest.mark.parametrize(
    "value", [float("inf"), float("-inf"), Decimal("Infinity")], ids=["inf", "-inf", "decimal-inf"]
)
def test_a_non_finite_number_in_the_blob_yields_an_empty_record(
    field_name: str, value: object
) -> None:
    assert SessionProvenance.from_state({"skus": [_sku_entry(**{field_name: value})]}).skus == {}


@pytest.mark.parametrize(
    "blob",
    [
        pytest.param({"orders": [123]}, id="order-id-is-an-int"),
        pytest.param({"orders": [None]}, id="order-id-is-none"),
        pytest.param({"carts": [["b1"]]}, id="cart-is-a-list"),
    ],
)
def test_a_non_string_id_in_the_blob_is_refused_rather_than_coerced(blob: dict[str, Any]) -> None:
    record = SessionProvenance.from_state(blob)
    assert record.orders == {} and record.carts == {}


# ------------------------------------------------------------------ 8. cap and eviction


@pytest.mark.parametrize(
    ("remember", "knows", "template"),
    [
        pytest.param(
            lambda r, i: r.remember_sku(
                i, unit_price_minor=1, currency="INR", catalogue_revision=1
            ),
            lambda r, i: r.knows_sku(i),
            "GRO-EVICT-{:04d}",
            id="skus",
        ),
        pytest.param(
            lambda r, i: r.remember_order_id(i),
            lambda r, i: r.knows_order(i),
            "ord-{:04d}",
            id="orders",
        ),
    ],
)
def test_eviction_is_oldest_first_and_reseeing_an_id_refreshes_its_age(
    remember: Any, knows: Any, template: str
) -> None:
    """The same LRU discipline must hold in every family, not only in SKUs."""
    record = SessionProvenance()
    for i in range(PROVENANCE_CAP + 10):
        remember(record, template.format(i))
    assert not knows(record, template.format(0))  # oldest ten evicted
    assert not knows(record, template.format(9))
    assert knows(record, template.format(10))  # the eleventh is now the oldest kept
    assert knows(record, template.format(PROVENANCE_CAP + 9))

    remember(record, template.format(10))  # re-seen: moves to newest
    remember(record, template.format(9999))  # forces one eviction
    assert knows(record, template.format(10))
    assert not knows(record, template.format(11))  # ... and it was the next-oldest that went


def test_a_hostile_read_loop_can_evict_an_id_but_only_into_a_hold() -> None:
    """Eviction is a usability cost, never a bypass -- so it is not a security bug.

    Injected content that talks the model into a long loop of searches can push the SKU the
    buyer actually wanted out of the record. What the attacker gets for that is a *hold* on
    the next write: the model is told to re-read, and the buyer sees one extra tool call.
    There is no ordering of remembers that turns eviction into a pass, because the only
    thing eviction can do to the record is make it smaller.
    """
    record = SessionProvenance()
    record.remember_sku(MILK_SKU, unit_price_minor=2800, currency="INR", catalogue_revision=3)
    assert check_sku_provenance(record, MILK_SKU) is None

    for i in range(PROVENANCE_CAP):  # the hostile loop
        record.remember_sku(
            f"GRO-NOISE-{i:04d}", unit_price_minor=1, currency="INR", catalogue_revision=1
        )

    held = check_sku_provenance(record, MILK_SKU)
    assert held is not None and held.reason_key == "sku_not_returned"
    assert bool(held.to_result()) is True
    assert len(record.skus) == PROVENANCE_CAP  # bounded, which is the other half of the point

    # And the cure is the ordinary one: read it again.
    record.remember_sku(MILK_SKU, unit_price_minor=2800, currency="INR", catalogue_revision=3)
    assert check_sku_provenance(record, MILK_SKU) is None


def test_eviction_cannot_resurrect_an_id_that_was_never_remembered() -> None:
    """Stated because the cap is the only code path that *deletes* from the record."""
    record = SessionProvenance()
    for i in range(PROVENANCE_CAP * 2):
        record.remember_order_id(f"ord-{i:04d}")
    assert check_order_provenance(record, "ord-never-existed") is not None
    assert len(record.orders) == PROVENANCE_CAP


def test_versions_within_one_checkout_are_capped_like_every_other_family() -> None:
    record = SessionProvenance()
    for version in range(1, PROVENANCE_CAP + 51):
        record.remember_approval(_approval(version, f"hash-{version}"))
    assert len(record.checkouts[CHECKOUT_ID].hashes_by_version) <= PROVENANCE_CAP


# ------------------------------------------------------------------------ 9. the lock


def test_two_sessions_do_not_share_a_write_lock() -> None:
    """A shared lock would let one session stall every other one: a cross-tenant denial."""

    async def probe() -> None:
        mine = session_write_lock("tenant-a:session-1")
        theirs = session_write_lock("tenant-b:session-1")
        assert mine is not theirs
        async with mine:
            assert mine.locked()
            assert not theirs.locked()
            # A second writer in the *same* session does share it, which is the point.
            assert session_write_lock("tenant-a:session-1") is mine

    asyncio.run(probe())


def test_a_session_id_that_is_empty_or_odd_still_gets_its_own_lock() -> None:
    """The key is opaque; no id may collapse into a shared default lock."""

    async def probe() -> None:
        blank = session_write_lock("")
        assert blank is not session_write_lock("0")
        assert blank is not session_write_lock(" ")
        assert session_write_lock("") is blank

    asyncio.run(probe())
