"""The buyer's cart has to be findable from the server, not only from one browser tab.

The defect this suite is written against was not subtle and was not rare. The cart id
lived in React state and nowhere else -- ``localStorage`` and ``sessionStorage`` were both
empty and no endpoint would answer "which cart is this buyer in" -- so a reload lost the
cart outright, and the copilot and the cart page could each be holding a different one.
"RazorAI said it added something and my cart is empty" was almost always that: the write
had landed, in a cart the page had stopped believing in.

``GET /v1/carts/current`` is the answer, and what is proven here is that it is a real
answer rather than a convenient one:

* an empty answer is a body, not a 404, because "this buyer has not started shopping" is
  not the same claim as "no such cart";
* the cart it returns is byte-for-byte the cart ``GET /v1/carts/{id}`` returns, so a
  surface has one parser and cannot be shown two different carts by two routes;
* with several open carts it returns the newest, deterministically, and touches none of
  the others;
* it is scoped to this buyer, not just to this tenant -- row-level security draws the
  tenant boundary and nothing but the query draws the buyer one;
* an agent session acting for the same buyer sees the same cart, which is the whole point:
  the copilot and the cart page stop disagreeing;
* a cart that has become a checkout is deliberately absent, and comes back the moment the
  buyer edits it and the kernel reopens it as a new version.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import MintedSession

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"
ATTA = "AASH-STPL-002"


# --------------------------------------------------------------------------- helpers


def _headers() -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}


def _open_basket(client: TestClient) -> str:
    response = client.post("/v1/carts", headers=_headers())
    assert response.status_code == 201, response.text
    return str(response.json()["cart_id"])


def _set_line(client: TestClient, cart_id: str, sku: str, quantity: int) -> dict[str, Any]:
    response = client.put(
        f"/v1/carts/{cart_id}/lines/{sku}",
        json={"quantity": quantity},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _current(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/carts/current")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _without_read_instant(cart: dict[str, Any]) -> dict[str, Any]:
    """The cart body with the wall-clock stamp of the read taken out of it.

    ``observed_at`` is when somebody looked, not anything about the cart. The quote's
    canonical content hash leaves it out for the same reason -- two reads a microsecond
    apart at one catalogue revision are the same cart -- so comparing two bodies means
    comparing everything except this.
    """
    stripped = {**cart, "freshness": {**cart["freshness"]}}
    stripped["freshness"].pop("observed_at")
    return stripped


# ---------------------------------------------------------------------------- tests


def test_a_buyer_with_no_cart_gets_an_empty_answer_rather_than_a_404(
    auth_client: TestClient,
) -> None:
    """A buyer who has not started shopping yet is an answer. A 404 is the wrong word.

    It would also be indistinguishable from the 404 a wrong cart id gets, which is the
    error a storefront reacts to by throwing its local state away.
    """
    assert _current(auth_client) == {"cart": None}


def test_the_path_is_not_parsed_as_a_basket_id(auth_client: TestClient) -> None:
    """The route ordering trap, pinned.

    ``/current`` and ``/{cart_id}`` are the same shape of URL and FastAPI matches in
    declaration order, so with the parameterised route first every request here would be
    answered 422 for a UUID that was never meant to be one.
    """
    assert auth_client.get("/v1/carts/current").status_code == 200
    assert auth_client.get(f"/v1/carts/{uuid.uuid4()}").status_code == 404


def test_the_cart_it_returns_is_the_cart_the_id_route_returns(auth_client: TestClient) -> None:
    """One cart, one body. Two routes that could disagree eventually would.

    ``freshness.observed_at`` is dropped from both sides before comparing. It is the
    instant of the read rather than anything about the cart, and the two reads happen
    microseconds apart; the catalogue revision beside it is the field that says whether
    they saw the same merchant state, and it is compared.
    """
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 2)
    _set_line(auth_client, cart_id, ATTA, 1)

    by_id = auth_client.get(f"/v1/carts/{cart_id}")
    assert by_id.status_code == 200, by_id.text
    assert _without_read_instant(_current(auth_client)["cart"]) == _without_read_instant(
        by_id.json()
    )


def test_the_cart_carries_its_lines_and_its_quote(auth_client: TestClient) -> None:
    """The point of returning the full body: the page can render from this one read."""
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 3)

    cart = _current(auth_client)["cart"]
    assert cart["cart_id"] == cart_id
    assert cart["lines"] == [{"sku": MILK, "quantity": 3}]
    assert cart["quote"] is not None
    assert cart["quote"]["lines"][0]["sku"] == MILK


def test_the_newest_open_cart_wins_and_the_older_ones_are_left_alone(
    auth_client: TestClient,
) -> None:
    """Several open carts is a real state, not a corrupt one: every POST makes another.

    The one the buyer was given last is the only one they can have been looking at, which
    is what makes newest the defensible answer. The older carts stay exactly as they were:
    closing them here would be a write on a read path, and one of them may be what another
    open tab is still holding.
    """
    first = _open_basket(auth_client)
    _set_line(auth_client, first, MILK, 1)
    second = _open_basket(auth_client)
    _set_line(auth_client, second, ATTA, 4)

    assert _current(auth_client)["cart"]["cart_id"] == second

    untouched = auth_client.get(f"/v1/carts/{first}")
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["lines"] == [{"sku": MILK, "quantity": 1}]

    # And it keeps answering the same way when asked twice: the order is by created_at with
    # the id breaking a tie, so it does not depend on which row PostgreSQL happens to reach
    # first.
    assert _current(auth_client)["cart"]["cart_id"] == second


def test_it_is_scoped_to_the_buyer_and_not_only_to_the_tenant(
    auth_client: TestClient,
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """Row-level security draws the tenant boundary. Nothing but this query draws the buyer one.

    Both sessions below are in the same tenant and the same merchant, so a query that
    leaned on row-level security alone would hand the second buyer the first buyer's cart.
    """
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 2)

    stranger_client, stranger = mint_client(buyer_ref=f"other-{uuid.uuid4().hex[:8]}")
    with stranger_client as stranger_authed:
        assert _current(stranger_authed) == {"cart": None}
        # The same 404 an unknown id gets, for the same reason: a 403 would confirm the
        # cart exists and turn this into an oracle for other buyers' carts.
        assert stranger_authed.get(f"/v1/carts/{cart_id}").status_code == 404

    assert _current(auth_client)["cart"]["cart_id"] == cart_id


def test_an_agent_acting_for_the_same_buyer_sees_the_same_cart(
    auth_client: TestClient,
    demo_session: MintedSession,
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """This is the disagreement the endpoint exists to end.

    The copilot could not find the cart the page was holding, so it built its own and told
    the buyer it had added something to "your cart". An agent holds ``catalogue.read``, and
    that is all this route asks for -- it grants no new authority, it only lets the thing
    that proposes a purchase look at the same cart the buyer is looking at.
    """
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 2)

    agent_client, agent = mint_client(actor_type="AGENT", buyer_ref=demo_session.buyer_ref)
    assert agent.actor_type == "AGENT"
    with agent_client as agent_authed:
        assert _current(agent_authed)["cart"]["cart_id"] == cart_id


def test_a_cart_that_has_become_a_checkout_is_not_offered_as_a_cart(
    auth_client: TestClient,
) -> None:
    """``CHECKED_OUT`` means an approval card is in front of the buyer.

    The surface for that is the checkout, not the cart, and answering with it here would
    invite a storefront to draw +/- buttons over lines somebody has already been asked to
    consent to. A caller that gets ``null`` while a checkout is open must go and read the
    checkout; taking the null as licence to start a fresh cart would strand the one being
    paid for.
    """
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 2)
    opened = auth_client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert opened.status_code == 201, opened.text

    assert _current(auth_client) == {"cart": None}
    # The cart itself is still readable by id. It is not gone; it is busy.
    assert auth_client.get(f"/v1/carts/{cart_id}").status_code == 200


def test_editing_a_checked_out_cart_brings_it_back_as_the_current_one(
    auth_client: TestClient,
) -> None:
    """The reopen path, seen from this endpoint.

    A cart write against a cart whose checkout is open invalidates that version, releases
    its hold and reopens the cart, and the next checkout is version N+1. This route follows
    that without knowing anything about it, because it reads the same ``status`` the reopen
    writes -- which is the check worth having: two things that must agree, agreeing.
    """
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 2)
    opened = auth_client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert opened.status_code == 201, opened.text
    assert opened.json()["version"] == 1
    assert _current(auth_client) == {"cart": None}

    _set_line(auth_client, cart_id, ATTA, 1)

    cart = _current(auth_client)["cart"]
    assert cart["cart_id"] == cart_id
    assert {line["sku"] for line in cart["lines"]} == {MILK, ATTA}

    reopened = auth_client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert reopened.status_code == 201, reopened.text
    assert reopened.json()["version"] == 2
