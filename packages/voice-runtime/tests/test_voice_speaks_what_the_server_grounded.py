"""The gateway must not refuse to speak a figure the server already proved.

THE DEFECT
----------
Two grounding checks run on one reply, and until now they disagreed.

The *server* checks a reply against ``agent_runtime.grounding.GroundingLedger``: every
amount, SKU and stock count that any tool returned during the whole turn. A sentence that
survives is a sentence the platform can prove.

The *gateway* then checked the same reply again, against a ledger it rebuilt itself by
walking the ``structured`` block for ``*_minor`` integers -- and ``structured`` is only the
LAST tool result of the turn. So a turn that read two products put one price in
``structured``, and the speech guard refused the sentence naming the other:

    guard refused model sentence (ungrounded_amount):
      'Yes, we have Amul Taaza Toned Milk 500 ml for Rs 28.00 and
       Amul Gold Full Cream Milk 1 L for Rs 73.00.'

The buyer saw the answer and heard almost none of it. It reads as broken speech synthesis,
and the synthesiser was never involved: the guard is working exactly as designed, on a
ledger narrower than the one that already cleared the sentence.

THE RULE
--------
The gateway may be **stricter than the model** -- that is its job -- but it may never be
stricter than the *server's own proof*. So the turn response carries the amounts the server
grounded, and the gateway trusts those rather than re-deriving a worse set. Nothing is
loosened: an amount that appears in neither ledger is still refused, and the guard's
authority is unchanged.
"""

from __future__ import annotations

from voice_runtime.gateway.agent_client import grounded_amounts


def test_a_price_in_the_structured_block_is_grounded() -> None:
    """The original behaviour, unchanged: figures in the payload still ground."""
    payload = {"structured": {"unit_price_minor": 7300, "unit_price": {"minor": 7300}}}
    assert 7300 in grounded_amounts(payload)


def test_every_price_the_turn_read_is_grounded_not_only_the_last_one() -> None:
    """The defect, at its narrowest.

    Two products were read; ``structured`` holds the second. The first product's price is
    the one the guard used to refuse, and it is the whole sentence a buyer loses.
    """
    payload = {
        "structured": {"kind": "product", "sku": "AMUL-DAIRY-002", "unit_price_minor": 7300},
        "grounded_amounts_minor": [2800, 7300],
    }
    amounts = grounded_amounts(payload)
    assert 7300 in amounts, "the last product's price must stay grounded"
    assert 2800 in amounts, "an earlier product's price is grounded too: a tool returned it"


def test_the_server_ledger_alone_is_enough() -> None:
    """A turn whose reply names figures no single structured block carries.

    A search that lists five products and names five prices is the ordinary case, and none
    of those prices need appear in ``structured`` at all.
    """
    payload = {"structured": {"kind": "products"}, "grounded_amounts_minor": [2800, 7300, 4500]}
    assert grounded_amounts(payload) == frozenset({2800, 7300, 4500})


def test_a_figure_in_neither_ledger_is_still_ungrounded() -> None:
    """The guard is not loosened. An amount nothing returned is refused as before."""
    payload = {"structured": {"unit_price_minor": 7300}, "grounded_amounts_minor": [7300]}
    assert 9900 not in grounded_amounts(payload)


def test_a_malformed_ledger_cannot_ground_anything() -> None:
    """Non-integers, booleans and nonsense in the field are ignored, not trusted.

    The field arrives over HTTP from another service. It is *evidence*, and evidence that
    does not parse grounds nothing rather than grounding everything -- a guard that failed
    open on a malformed payload would be worse than no guard.
    """
    for bad in ("7300", None, {"minor": 7300}, [True, False], ["7300"], [None], 7300):
        payload = {"structured": {"unit_price_minor": 4500}, "grounded_amounts_minor": bad}
        assert grounded_amounts(payload) == frozenset({4500}), f"grounded from {bad!r}"
