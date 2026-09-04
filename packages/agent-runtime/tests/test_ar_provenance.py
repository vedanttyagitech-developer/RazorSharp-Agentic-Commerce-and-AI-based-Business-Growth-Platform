"""Provenance: a write may name only what a tool returned this session.

One case per write gate, the 200-cap eviction, state round-trip, and the concurrency
case that motivates the lock: two ``basket_set_line`` calls in one round must not
interleave their read-compute-write.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from agent_runtime.backends import InMemoryBackend, InMemoryTrustedSurface
from agent_runtime.backends.base import ProductCard, Provenance
from agent_runtime.core import (
    MAX_BASKET_LINES,
    MAX_LINE_QUANTITY,
    PROVENANCE_CAP,
    Held,
    SessionProvenance,
    check_checkout_provenance,
    check_line_count,
    check_order_provenance,
    check_proposal_provenance,
    check_quantity,
    check_sku_provenance,
    session_write_lock,
)
from commerce_domain import Money
from merchant_sim import Locale

from .conftest import MILK_SKU

ATTA_SKU = "GRO-STPL-002"


def _card(sku: str, minor: int = 2800) -> ProductCard:
    return ProductCard(
        sku=sku,
        name=sku,
        description="",
        category="dairy",
        unit_label="500 ml",
        unit_price=Money(minor, "INR"),
        stock_units=5,
        is_listed=True,
        is_available=True,
        provenance=Provenance("sim", 3, datetime.now(UTC)),
    )


# ------------------------------------------------------------------------ held shape


def test_a_held_result_is_never_an_empty_dict() -> None:
    """ADK treats ``{}`` from a callback as "run the tool"; a hold must never look like that."""
    held = check_sku_provenance(SessionProvenance(), "GRO-FAKE-999")
    assert isinstance(held, Held)
    result = held.to_result()
    assert result and result["ok"] is False
    assert result["blocked"] == "provenance"
    assert result["reason_key"] == "sku_not_returned"
    assert "GRO-FAKE-999" in result["instruction"]
    assert result["sku"] == "GRO-FAKE-999"


# ------------------------------------------------------------------------ sku gate


def test_basket_write_accepts_only_a_sku_a_tool_returned() -> None:
    record = SessionProvenance()
    assert check_sku_provenance(record, MILK_SKU) is not None
    record.remember_product(_card(MILK_SKU))
    assert check_sku_provenance(record, MILK_SKU) is None
    assert check_sku_provenance(record, MILK_SKU.lower()) is None  # ids are case-insensitive
    assert check_sku_provenance(record, ATTA_SKU) is not None


def test_a_line_already_in_the_basket_passes_without_a_fresh_read() -> None:
    """A returning buyer's basket predates the session; removing a line needs no search."""
    record = SessionProvenance()
    assert check_sku_provenance(record, ATTA_SKU, basket_lines=(ATTA_SKU,)) is None
    assert check_sku_provenance(record, MILK_SKU, basket_lines=(ATTA_SKU,)) is not None


@pytest.mark.asyncio
async def test_search_and_basket_results_ground_their_skus(backend: InMemoryBackend) -> None:
    record = SessionProvenance()
    page = await backend.search("doodh", Locale.HI_LATN, 3)
    record.remember_search(page)
    assert page.hits and all(record.knows_sku(hit.sku) for hit in page.hits)
    seen = record.skus[page.hits[0].sku]
    assert seen.unit_price_minor == page.hits[0].unit_price.minor

    basket = await backend.basket_create()
    view = await backend.basket_set_line(basket.basket_id, ATTA_SKU, 2)
    record.remember_basket(view)
    assert record.knows_basket(basket.basket_id)
    assert record.knows_sku(ATTA_SKU)


# ------------------------------------------------------------------- checkout gate


@pytest.mark.asyncio
async def test_submit_accepts_only_a_version_and_hash_the_session_was_shown(
    backend: InMemoryBackend,
) -> None:
    record = SessionProvenance()
    basket = await backend.basket_create()
    await backend.basket_set_line(basket.basket_id, MILK_SKU, 2)
    card = await backend.checkout_create(basket.basket_id)

    unknown = check_checkout_provenance(record, card.checkout_id, 1, card.content_hash)
    assert unknown is not None and unknown.reason_key == "checkout_not_returned"

    record.remember_approval(card)
    assert check_checkout_provenance(record, card.checkout_id, 1, card.content_hash) is None
    wrong_version = check_checkout_provenance(record, card.checkout_id, 2, card.content_hash)
    assert wrong_version is not None and wrong_version.reason_key == "checkout_version_not_returned"
    wrong_hash = check_checkout_provenance(record, card.checkout_id, 1, "deadbeef")
    assert wrong_hash is not None and wrong_hash.reason_key == "content_hash_mismatch"


@pytest.mark.asyncio
async def test_a_checkout_read_grounds_every_version_it_carries(backend: InMemoryBackend) -> None:
    record = SessionProvenance()
    basket = await backend.basket_create()
    await backend.basket_set_line(basket.basket_id, MILK_SKU, 1)
    card = await backend.checkout_create(basket.basket_id)
    view = await backend.checkout_get(card.checkout_id)
    record.remember_checkout(view)
    assert check_checkout_provenance(record, card.checkout_id, 1, card.content_hash) is None


# ------------------------------------------------------- order and proposal gates


@pytest.mark.asyncio
async def test_order_gate(backend: InMemoryBackend, surface: InMemoryTrustedSurface) -> None:
    record = SessionProvenance()
    basket = await backend.basket_create()
    await backend.basket_set_line(basket.basket_id, MILK_SKU, 1)
    card = await backend.checkout_create(basket.basket_id)
    surface.approve(
        card.checkout_id, 1, content_hash=card.content_hash, total_minor=card.total.minor
    )
    await backend.checkout_submit_approved(card.checkout_id, 1, card.content_hash)
    surface.record_provider_capture(card.checkout_id)
    order_id = surface.order_id_for(card.checkout_id)
    assert order_id is not None

    held = check_order_provenance(record, order_id)
    assert held is not None and held.reason_key == "order_not_returned"
    record.remember_order(await backend.order_track(order_id))
    assert check_order_provenance(record, order_id) is None
    assert record.knows_sku(MILK_SKU)  # the order's lines count as provenance for a reorder


def test_an_order_id_handed_over_by_the_trusted_surface_counts() -> None:
    record = SessionProvenance()
    record.remember_order_id("o-from-screen")
    assert check_order_provenance(record, "o-from-screen") is None


def test_proposal_gate() -> None:
    record = SessionProvenance()
    held = check_proposal_provenance(record, "p1")
    assert held is not None and held.reason_key == "proposal_not_returned"
    record.remember_proposal("p1")
    assert check_proposal_provenance(record, "p1") is None


# --------------------------------------------------------------- quantity and lines


@pytest.mark.parametrize("quantity", [0, 1, MAX_LINE_QUANTITY])
def test_quantity_within_the_cap_passes(quantity: int) -> None:
    assert check_quantity(quantity) is None


@pytest.mark.parametrize(
    ("quantity", "reason"),
    [
        (MAX_LINE_QUANTITY + 1, "quantity_exceeds_cap"),
        (-1, "invalid_quantity"),
        (True, "invalid_quantity"),
        (2.5, "invalid_quantity"),
        ("3", "invalid_quantity"),
    ],
)
def test_quantity_outside_the_cap_or_not_an_integer_is_held(quantity: object, reason: str) -> None:
    held = check_quantity(quantity)  # type: ignore[arg-type]
    assert held is not None and held.reason_key == reason
    assert held.to_result()["blocked"] == "quantity"


def test_line_count_cap_holds_only_a_new_line_on_a_full_basket() -> None:
    full = [f"GRO-TEST-{i:03d}" for i in range(MAX_BASKET_LINES)]
    held = check_line_count(full, "GRO-NEW-001", 1)
    assert held is not None and held.reason_key == "basket_full"
    assert check_line_count(full, full[0], 5) is None  # changing an existing line
    assert check_line_count(full, "GRO-NEW-001", 0) is None  # a removal never fills
    assert check_line_count(full[:-1], "GRO-NEW-001", 1) is None


# ------------------------------------------------------------------- cap and state


def test_the_record_keeps_the_newest_200_and_reseeing_refreshes_age() -> None:
    record = SessionProvenance()
    for i in range(PROVENANCE_CAP + 50):
        record.remember_product(_card(f"GRO-TEST-{i:04d}"))
    assert len(record.skus) == PROVENANCE_CAP
    assert not record.knows_sku("GRO-TEST-0000")
    assert record.knows_sku(f"GRO-TEST-{PROVENANCE_CAP + 49:04d}")
    oldest_kept = f"GRO-TEST-{50:04d}"
    assert record.knows_sku(oldest_kept)
    record.remember_product(_card(oldest_kept))  # re-seen: now the newest
    record.remember_product(_card("GRO-TEST-9999"))
    assert record.knows_sku(oldest_kept)
    assert not record.knows_sku(f"GRO-TEST-{51:04d}")


def test_state_round_trip_is_json_safe_and_preserves_age() -> None:
    record = SessionProvenance()
    record.remember_product(_card(MILK_SKU))
    record.remember_product(_card(ATTA_SKU, 25500))
    record.remember_order_id("o1")
    record.remember_proposal("p1")
    from agent_runtime.backends.base import ApprovalCard, BasketQuote, CheckoutStatus, PricedLine

    quote = BasketQuote(
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
        content_hash="h1",
        provenance=Provenance("sim", 1),
    )
    record.remember_approval(ApprovalCard("c1", 1, "h1", CheckoutStatus.PENDING_APPROVAL, quote))

    blob = json.loads(json.dumps(record.to_state()))
    restored = SessionProvenance.from_state(blob)
    assert list(restored.skus) == [MILK_SKU, ATTA_SKU]
    assert restored.skus[ATTA_SKU].unit_price_minor == 25500
    assert restored.knows_order("o1") and restored.knows_proposal("p1")
    assert check_checkout_provenance(restored, "c1", 1, "h1") is None


_MALFORMED_BLOBS: list[object] = [
    None,
    "junk",
    [],
    {"skus": "nope"},
    {"skus": [{"sku": 1}]},
    {"checkouts": [{"checkout_id": "c", "versions": "x"}]},
]


@pytest.mark.parametrize("blob", _MALFORMED_BLOBS)
def test_a_malformed_state_blob_yields_an_empty_record(blob: object) -> None:
    """Empty is the safe direction: every write is then held until a fresh read."""
    restored = SessionProvenance.from_state(blob)
    assert not restored.skus and not restored.checkouts and not restored.orders


# ---------------------------------------------------------------------- the lock


@pytest.mark.asyncio
async def test_two_concurrent_basket_writes_in_one_round_do_not_interleave(
    backend: InMemoryBackend,
) -> None:
    """Read-compute-write under the session lock runs alone; without it, one write wins silently."""
    basket = await backend.basket_create()
    trace: list[str] = []

    async def gated_add(sku: str, quantity: int) -> None:
        async with session_write_lock("session-1"):
            trace.append(f"read:{sku}")
            current = await backend.basket_get(basket.basket_id)
            await asyncio.sleep(
                0
            )  # yield mid-critical-section, where an unlocked write would slip in
            existing = dict(current.lines).get(sku, 0)
            trace.append(f"write:{sku}")
            await backend.basket_set_line(basket.basket_id, sku, existing + quantity)

    await asyncio.gather(gated_add(MILK_SKU, 1), gated_add(MILK_SKU, 2), gated_add(ATTA_SKU, 1))
    # Every read is immediately followed by its own write: no interleaving.
    for i in range(0, len(trace), 2):
        assert trace[i].startswith("read:") and trace[i + 1] == trace[i].replace("read:", "write:")
    view = await backend.basket_get(basket.basket_id)
    assert dict(view.lines) == {MILK_SKU: 3, ATTA_SKU: 1}


@pytest.mark.asyncio
async def test_locks_are_per_session_and_do_not_leak() -> None:
    a = session_write_lock("s-a")
    b = session_write_lock("s-b")
    assert a is not b
    assert session_write_lock("s-a") is a
    async with a:
        assert session_write_lock("s-a").locked()
        assert not session_write_lock("s-b").locked()
    del a, b
    assert not session_write_lock("s-a").locked()
