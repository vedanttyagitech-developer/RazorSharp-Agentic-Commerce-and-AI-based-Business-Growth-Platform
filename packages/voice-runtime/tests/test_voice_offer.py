"""What a spoken "yes" refers to: the one product the reply put forward."""

from __future__ import annotations

from voice_runtime.gateway.agent_client import offer_in

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
