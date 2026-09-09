"""Showing a product and asking for it added are different events, and the wire says which.

``offer`` is set for both. A ``basket.update`` proposal names a SKU and a delta, and a
product page names its first row, and ``offer_in`` returns one shape for either -- which is
right, because a spoken "yes" refers to that product in both cases.

What was missing is the difference. A surface acting on ``offer`` alone puts something in
the buyer's basket every time they ask to *see* a product, and the buyer reported exactly
the other half of it: they said "add two milk", heard the assistant agree, and nothing was
added. The gateway knew -- ``_line_proposal`` is how it built the offer in the first place
-- and simply never said so.

So ``offer_is_proposal`` travels on the reply. True means the buyer asked for it and the
trusted surface may add it: that is their own instruction, and adding to a basket is not
consent to buy. False means show it and wait.
"""

from __future__ import annotations

from voice_runtime.gateway.agent_client import offer_in
from voice_runtime.wire.frames import AgentReply

PRODUCT = {
    "kind": "product",
    "sku": "AMUL-DAIRY-001",
    "display_name": "Amul Taaza Toned Milk 500 ml",
    "unit_price": {"minor": 2800, "currency": "INR", "display": "28.00"},
    "stock_units": 40,
    "is_available": True,
}

PROPOSAL = {
    **PRODUCT,
    "proposal": {
        "kind": "proposal",
        "action": "basket.update",
        "sku": "AMUL-DAIRY-001",
        "delta": 2,
        "display": {
            "name": "Amul Taaza Toned Milk 500 ml",
            "unit_price": {"minor": 2800, "currency": "INR", "display": "28.00"},
        },
    },
}


def _reply(structured: dict[str, object]) -> AgentReply:
    from voice_runtime.gateway.agent_client import _line_proposal

    return AgentReply(
        text="ok",
        deterministic=False,
        locale="en-IN",
        turn_id=1,
        speech_generation=1,
        offer=offer_in(structured),
        offer_is_proposal=_line_proposal(structured) is not None,
    )


def test_a_product_merely_shown_is_not_a_proposal() -> None:
    """The regression that would put things in a basket nobody asked to fill."""
    reply = _reply(PRODUCT)
    assert reply.offer is not None, "the product is still the thing a spoken yes refers to"
    assert reply.offer["sku"] == "AMUL-DAIRY-001"
    assert reply.offer_is_proposal is False


def test_an_add_request_is_a_proposal_and_carries_its_quantity() -> None:
    reply = _reply(PROPOSAL)
    assert reply.offer_is_proposal is True
    assert reply.offer is not None
    assert reply.offer["sku"] == "AMUL-DAIRY-001"
    # The delta, not a default of one: "add two" must add two.
    assert reply.offer["quantity"] == 2


def test_the_flag_defaults_to_not_proposing() -> None:
    """A frame built without the field shows a product; it does not fill a basket.

    The safe direction, and deliberately the default: a surface that adds on every reply
    is a worse failure than one that adds on none, because the second is visible and the
    first spends the buyer's money on things they only asked about.
    """
    reply = AgentReply(
        text="ok", deterministic=False, locale="en-IN", turn_id=1, speech_generation=1
    )
    assert reply.offer_is_proposal is False
