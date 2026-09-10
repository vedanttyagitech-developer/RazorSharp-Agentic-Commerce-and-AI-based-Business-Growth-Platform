"""What a spoken "yes" refers to, and what the page draws: the products a reply put forward.

``offer_in`` is the one product a "yes" takes; ``items_in`` is the shelf the page shows,
read from the same rows in the same order, so the first item is always the offer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from voice_runtime.clock import FakeClock
from voice_runtime.gateway.agent_client import MAX_ITEMS, HttpTurnHandler, items_in, offer_in
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.testing import (
    CHECKOUT_STATE_DECISION_CARD,
    STRUCTURED,
    MemoryTransport,
    an_identity,
)
from voice_runtime.tts.synth import FakeSynthesizer
from voice_runtime.turn import FakeTurnHandler, TurnReply

_MILK = {
    "sku": "AMUL-DAIRY-002",
    "display_name": "Amul Gold Full Cream Milk 1 L",
    "is_available": True,
    "unit_price": {"minor": 7300, "currency": "INR", "display": "73.00"},
}


def test_a_product_card_is_an_offer_of_one() -> None:
    assert offer_in({"kind": "product", "product": _MILK}) == {
        "sku": "AMUL-DAIRY-002",
        "name": "Amul Gold Full Cream Milk 1 L",
        "quantity": 1,
        "unit_price": _MILK["unit_price"],
    }


def test_a_search_page_offers_its_first_hit() -> None:
    offer = offer_in({"kind": "products", "hits": [_MILK, {"sku": "X", "display_name": "Other"}]})
    assert offer is not None and offer["sku"] == "AMUL-DAIRY-002"


def test_a_line_proposal_carries_its_quantity() -> None:
    structured = {
        "kind": "product",
        "product": _MILK,
        "proposal": {
            "action": "basket.update",
            "sku": "AMUL-DAIRY-002",
            "delta": 2,
            "display": {"name": "Amul Gold Full Cream Milk 1 L", "unit_price": _MILK["unit_price"]},
        },
    }
    assert offer_in(structured) == {
        "sku": "AMUL-DAIRY-002",
        "name": "Amul Gold Full Cream Milk 1 L",
        "quantity": 2,
        "cart_id": None,
        "absolute_quantity": None,
        "blocked_by": None,
        "binding": None,
        "unit_price": _MILK["unit_price"],
    }


def test_nothing_offered_means_none() -> None:
    assert offer_in(None) is None
    assert offer_in({"kind": "basket", "basket": {}}) is None
    assert offer_in({"kind": "product", "product": {**_MILK, "is_available": False}}) is None


def test_the_bridged_runner_spreads_the_card_flat() -> None:
    flat = {"kind": "product", **_MILK, "stock_units": 30}
    assert offer_in(flat) == {
        "sku": "AMUL-DAIRY-002",
        "name": "Amul Gold Full Cream Milk 1 L",
        "quantity": 1,
        "unit_price": _MILK["unit_price"],
    }
    assert offer_in({"kind": "product", **_MILK, "stock_units": 0}) is None


def test_a_search_page_offers_the_product_the_sentence_leads_with() -> None:
    other = {
        "sku": "MOTH-DAIRY-011",
        "display_name": "Mother Dairy Full Cream Milk 1 L",
        "is_available": True,
    }
    page = {"kind": "products", "hits": [_MILK, other]}
    text = (
        "I'd go with **Mother Dairy Full Cream Milk 1 L** today; "
        "Amul Gold Full Cream Milk 1 L is the other option."
    )
    offer = offer_in(page, text)
    assert offer is not None and offer["sku"] == "MOTH-DAIRY-011"
    assert offer_in(page, "Two options are in stock.")["sku"] == "AMUL-DAIRY-002"


# ---- the shelf: every product the reply put on the page ---------------------------------

_OTHER = {
    "sku": "MOTH-DAIRY-011",
    "display_name": "Mother Dairy Full Cream Milk 1 L",
    "is_available": True,
    "stock_units": 12,
    "unit_price": {"minor": 6800, "currency": "INR", "display": "68.00"},
}


def _milk_item(**overrides: Any) -> dict[str, Any]:
    return {
        "sku": "AMUL-DAIRY-002",
        "name": "Amul Gold Full Cream Milk 1 L",
        "unit_price": _MILK["unit_price"],
        "stock_units": None,
        "available": True,
        **overrides,
    }


def test_a_product_card_is_a_shelf_of_one() -> None:
    assert items_in({"kind": "product", "product": _MILK}) == [_milk_item()]


def test_the_flat_card_carries_its_shelf_count_onto_the_item() -> None:
    flat = {"kind": "product", **_MILK, "stock_units": 30}
    assert items_in(flat) == [_milk_item(stock_units=30)]


def test_a_search_page_lists_its_hits_in_order_unless_the_sentence_leads_elsewhere() -> None:
    third = {"sku": "X-3", "display_name": "Third Thing", "is_available": True}
    page = {"kind": "products", "hits": [_MILK, _OTHER, third]}

    assert [item["sku"] for item in items_in(page, "Two options are in stock.")] == [
        "AMUL-DAIRY-002",
        "MOTH-DAIRY-011",
        "X-3",
    ]
    # The one named first moves up; the rest keep the search engine's order.
    led = items_in(page, "Try **Third Thing**, or Mother Dairy Full Cream Milk 1 L.")
    assert [item["sku"] for item in led] == ["X-3", "AMUL-DAIRY-002", "MOTH-DAIRY-011"]


def test_the_first_item_is_always_the_offer() -> None:
    page = {"kind": "products", "hits": [_MILK, _OTHER]}
    for text in ("Two options.", "I'd go with Mother Dairy Full Cream Milk 1 L today."):
        offer = offer_in(page, text)
        assert offer is not None
        assert items_in(page, text)[0]["sku"] == offer["sku"]


def test_a_long_page_is_cut_to_five_after_the_lead_moves_up() -> None:
    hits = [
        {"sku": f"S-{n}", "display_name": f"Product {n}", "is_available": True} for n in range(8)
    ]
    page = {"kind": "products", "hits": hits}
    assert [item["sku"] for item in items_in(page)] == ["S-0", "S-1", "S-2", "S-3", "S-4"]
    assert MAX_ITEMS == 5
    # The seventh is what the sentence recommends: it must survive the cut, at the front.
    led = items_in(page, "Product 7 is the pick.")
    assert [item["sku"] for item in led] == ["S-7", "S-0", "S-1", "S-2", "S-3"]


def test_a_sold_out_product_stays_on_the_shelf_but_is_never_the_offer() -> None:
    sold_out = {**_MILK, "is_available": False, "stock_units": 0}
    none_left = {**_OTHER, "stock_units": 0}
    page = {"kind": "products", "hits": [sold_out, none_left, {"sku": "X-3", "stock_units": 4}]}

    items = items_in(page)
    assert [(item["sku"], item["available"]) for item in items] == [
        ("AMUL-DAIRY-002", False),
        ("MOTH-DAIRY-011", False),
        ("X-3", True),
    ]
    assert items[0]["stock_units"] == 0
    assert items[2]["name"] == "X-3", "a row with no name is shown by its sku"
    assert items[2]["unit_price"] is None
    # The shelf keeps what cannot be bought; a "yes" to it takes nothing.
    assert offer_in(page) is None
    assert items_in({"kind": "product", "product": sold_out}) == [
        _milk_item(stock_units=0, available=False)
    ]


def test_a_line_proposal_narrows_the_shelf_to_the_proposed_product() -> None:
    proposal = {
        "action": "basket.update",
        "sku": "MOTH-DAIRY-011",
        "delta": 2,
        "display": {
            "name": "Mother Dairy Full Cream Milk 1 L",
            "unit_price": _OTHER["unit_price"],
            "stock_units": 12,
        },
    }
    # With the card it was prepared from: the item is read off the card, shelf count and all.
    with_card = {"kind": "products", "hits": [_MILK, _OTHER], "proposal": proposal}
    assert items_in(with_card) == [
        {
            "sku": "MOTH-DAIRY-011",
            "name": "Mother Dairy Full Cream Milk 1 L",
            "unit_price": _OTHER["unit_price"],
            "stock_units": 12,
            "available": True,
        }
    ]
    # Without it: the proposal's own display is the product.
    alone = {"kind": "basket", "basket": {}, "proposal": proposal}
    assert items_in(alone) == items_in(with_card)
    assert offer_in(alone) is not None and offer_in(alone)["quantity"] == 2


def test_a_turn_that_showed_no_product_has_an_empty_shelf() -> None:
    assert items_in(None) == []
    assert items_in("not a payload") == []
    assert items_in({"kind": "basket", "basket": {"lines": []}}) == []
    assert items_in({"kind": "checkout", "quote": {"total_minor": 39500}}) == []
    assert items_in({"kind": "products", "hits": "not a list"}) == []
    assert items_in({"kind": "products", "hits": [{"display_name": "no sku"}]}) == []


def test_an_amount_is_carried_only_as_the_apis_own_money_object() -> None:
    """Nothing here formats money: a bare integer is not a price the page may show."""
    bare = {"kind": "product", "sku": "X", "unit_price": 7300, "stock_units": True}
    assert items_in(bare) == [
        {"sku": "X", "name": "X", "unit_price": None, "stock_units": None, "available": True}
    ]


def test_the_http_reply_carries_the_shelf_beside_the_offer() -> None:
    reply = HttpTurnHandler._to_reply(  # noqa: SLF001 - the seam under test
        {"reply": "Amul Gold 1 L is ₹73.", "language": "en", "structured": STRUCTURED}
    )
    assert reply.items is not None
    assert [item["sku"] for item in reply.items] == ["AMUL-DAIRY-002", "AMUL-DAIRY-003"]
    assert reply.items[0]["unit_price"] == STRUCTURED["hits"][0]["unit_price"]
    assert reply.items[0]["stock_units"] == 30
    assert reply.offer is not None and reply.offer["sku"] == reply.items[0]["sku"]

    nothing = HttpTurnHandler._to_reply({"reply": "Your basket is empty.", "structured": None})  # noqa: SLF001
    assert nothing.items == [] and nothing.offer is None


# ---- on the wire: a list on every conversational reply, None on a money sentence ---------


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _spoken(transport: MemoryTransport, count: int) -> Callable[[], bool]:
    """True once ``count`` utterances have finished speaking."""
    return lambda: len(transport.frames("speech_end")) == count


async def _frames_for(*replies: TurnReply) -> list[dict[str, Any]]:
    transport = MemoryTransport()
    pipeline = VoicePipeline(
        transport=transport,
        stt_factory=FakeSttFactory(),
        synthesizer=FakeSynthesizer(),
        turn_handler=FakeTurnHandler(replies=list(replies)),
        identity=an_identity(),
        clock=FakeClock(),
        rotation_margin_s=10_000.0,
        connect_timeout_s=1.0,
        backoff_start_s=0.0,
    )
    task = asyncio.create_task(pipeline.run())
    try:
        for n in range(len(replies)):
            transport.push_text({"type": "text_input", "text": f"turn {n}"})
            await _wait_until(_spoken(transport, n + 1))
    finally:
        transport.end()
        await task
    return transport.frames("agent_reply")


@pytest.mark.asyncio
async def test_a_conversational_reply_puts_its_shelf_on_the_wire() -> None:
    shelf = items_in({"kind": "products", "hits": [_MILK, _OTHER]})
    offer = offer_in({"kind": "products", "hits": [_MILK, _OTHER]})
    (frame,) = await _frames_for(
        TurnReply(text="Two milks are in stock.", items=shelf, offer=offer)
    )
    assert frame["deterministic"] is False
    assert frame["items"] == shelf
    assert frame["offer"] == offer


@pytest.mark.asyncio
async def test_a_reply_that_names_no_shelf_sends_an_empty_one_not_nothing() -> None:
    """The page redraws from the frame alone, so "no products" has to be said, not implied."""
    (frame,) = await _frames_for(TurnReply(text="Your basket is empty."))
    assert frame["items"] == []
    assert frame["offer"] is None


@pytest.mark.asyncio
async def test_a_money_sentence_carries_no_shelf_and_no_offer() -> None:
    (frame,) = await _frames_for(TurnReply(decision_card=CHECKOUT_STATE_DECISION_CARD))
    assert frame["deterministic"] is True
    assert frame["items"] is None
    assert frame["offer"] is None
