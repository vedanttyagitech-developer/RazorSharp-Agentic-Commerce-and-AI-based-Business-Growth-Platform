"""Taking a cart back from its own checkout: who may, when, and what it costs.

Opening a checkout closes the cart, and every later line write used to answer 409 "start a
new one". That reads as a small rule and behaves as a dead end: the assistant offers a
second item at the approval card -- which is exactly where a shop would offer one -- the
buyer says yes, and the write fails against a cart that has become a checkout. The buyer is
left holding an approval for an order they no longer want and a suggestion they cannot
accept.

So the cart comes back, and the version they were looking at ends: version N is invalidated
and its hold released, the cart returns to OPEN, the write lands, and the next checkout is
version N+1 on the same checkout row with its own content, its own hash and its own
approval. Nothing the buyer already consented to is reused for a different total, which is
the entire point of binding an approval to bytes.

Three properties are load-bearing and each has a test here.

**Only the buyer may do it.** ``set_line`` is gated on ``basket.write``, which agents hold
because building a cart is theirs to do. Approving, rejecting and cancelling are withheld
from them on the stated principle that consent is not delegable to the thing that proposed
the purchase -- and a line write that quietly retires a recorded approval is a reject
wearing different clothes. The supersede therefore also requires ``checkout.cancel``.

**Only before the money moves.** From EXECUTION_PENDING a grant is issued and a provider
order may exist, and no edit to a cart may reach around that.

**And the refusal must not carry ``basket_status``.** The storefront reads any 409 with that
key as "this cart is gone" and recovers by opening a fresh cart and replaying the single
failed line into it. On a cart whose payment is in flight that would hand the buyer a new
cart containing one item and silently drop the rest.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import MintedSession

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"
POPCORN = "ACT-SNCK-019"


def _headers() -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}


def _cart_with_a_line(client: TestClient) -> str:
    created = client.post("/v1/carts", headers=_headers())
    assert created.status_code == 201, created.text
    cart_id = str(created.json()["cart_id"])
    line = client.put(f"/v1/carts/{cart_id}/lines/{MILK}", json={"quantity": 1}, headers=_headers())
    assert line.status_code == 200, line.text
    return cart_id


def _open_checkout(client: TestClient, cart_id: str) -> dict[str, Any]:
    opened = client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert opened.status_code == 201, opened.text
    card: dict[str, Any] = opened.json()
    return card


def test_a_buyer_may_add_at_the_approval_card_and_gets_a_new_version(
    auth_client: TestClient,
) -> None:
    """The whole point. A second item at the card produces N+1, not a refusal."""
    cart_id = _cart_with_a_line(auth_client)
    first = _open_checkout(auth_client, cart_id)

    added = auth_client.put(
        f"/v1/carts/{cart_id}/lines/{POPCORN}", json={"quantity": 1}, headers=_headers()
    )
    assert added.status_code == 200, added.text

    second = _open_checkout(auth_client, cart_id)
    # One checkout per cart is a database constraint, so this must be the same row.
    assert second["checkout_id"] == first["checkout_id"]
    assert second["version"] == first["version"] + 1
    # New bytes, and therefore a new hash: the approval the buyer might have given for the
    # first version cannot be spent on this one.
    assert second["content_hash"] != first["content_hash"]
    assert second["amount_minor"] > first["amount_minor"]


def test_asking_to_check_out_again_is_not_a_dead_end(auth_client: TestClient) -> None:
    """A buyer who walked away from the card and came back gets a fresh version.

    Written because the two entry points disagreed: a line write reopened the cart and
    opening a checkout did not, so whether the shop could answer depended on whether the
    buyer happened to change something first -- a rule nobody could have predicted.
    """
    cart_id = _cart_with_a_line(auth_client)
    first = _open_checkout(auth_client, cart_id)
    again = _open_checkout(auth_client, cart_id)
    assert again["checkout_id"] == first["checkout_id"]
    assert again["version"] == first["version"] + 1


def test_an_agent_may_not_retire_the_buyers_approval_with_a_line_write(
    demo_session: MintedSession,
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """An agent writing a line must not be able to end a version the buyer was shown.

    The capability table withholds approve, reject and cancel from agents deliberately:
    consent is not delegable to the thing that proposed the purchase. Superseding a version
    the buyer is looking at is a reject in all but name, so it needs the buyer's own
    authority even though the route it arrives on is a cart write.
    """
    buyer_client, _ = mint_client(actor_type="BUYER", buyer_ref=demo_session.buyer_ref)
    with buyer_client as buyer:
        cart_id = _cart_with_a_line(buyer)
        card = _open_checkout(buyer, cart_id)

    agent_client, agent = mint_client(actor_type="AGENT", buyer_ref=demo_session.buyer_ref)
    assert agent.actor_type == "AGENT"
    with agent_client as acting_agent:
        refused = acting_agent.put(
            f"/v1/carts/{cart_id}/lines/{POPCORN}", json={"quantity": 1}, headers=_headers()
        )
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["reason"] == "approval_retirement_not_delegable"
    assert body["checkout_state"] == "APPROVAL_REQUIRED"

    # And the version the buyer was shown is untouched: same version, still awaiting them.
    with buyer_client as buyer:
        checkout = buyer.get(f"/v1/checkouts/{card['checkout_id']}")
    assert checkout.status_code == 200, checkout.text
    read = checkout.json()
    assert read["current_version"] == card["version"]
    assert read["state"] == "APPROVAL_REQUIRED"
    assert read["approval_card"]["content_hash"] == card["content_hash"]


def test_a_cart_whose_payment_is_in_flight_is_refused_without_basket_status(
    auth_client: TestClient,
) -> None:
    """Money may be moving against those exact lines, so the answer is no.

    The shape of the no matters as much as the no. ``basket_status`` in a 409 tells the
    storefront the cart is gone, and it recovers by opening a new one and replaying only
    the line that failed -- which on a cart mid-payment would drop everything else the
    buyer had.
    """
    cart_id = _cart_with_a_line(auth_client)
    card = _open_checkout(auth_client, cart_id)

    admitted = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve-and-pay",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers=_headers(),
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["allowed"] is True

    refused = auth_client.put(
        f"/v1/carts/{cart_id}/lines/{POPCORN}", json={"quantity": 1}, headers=_headers()
    )
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["reason"] == "payment_in_flight"
    assert "basket_status" not in body
    assert body["checkout_state"] in {"EXECUTION_PENDING", "AWAITING_PAYMENT"}


def test_a_cart_comes_back_after_a_confirmed_payment_failure(
    auth_client: TestClient,
    capi_kernel_engine: Any,
    seeded_tenant: Any,
) -> None:
    """A confirmed failure is not a payment that may be going through.

    The refusal above is right about EXECUTION_PENDING and AWAITING_PAYMENT: a grant is
    issued, a provider order may exist, and no edit to a cart may reach around that. It was
    being applied to four states where nothing is moving at all, and the kernel's own
    transition table says so -- ``PAYMENT_FAILED -> INVALIDATED`` is an edge it allows,
    with the comment "a confirmed failure releases the reservation".

    What that cost a buyer: a declined payment ended their cart forever. Every later "add
    milk" answered "a payment may be going through" about a payment the provider had
    already refused, and the only way out was knowing to say "start a new cart" -- a
    sentence nothing on the screen tells them.
    """
    cart_id = _cart_with_a_line(auth_client)
    card = _open_checkout(auth_client, cart_id)

    # Put the version where the provider or the buyer would have put it. Written directly
    # because the routes that reach these states need a provider answer this test has no
    # way to produce; the subject here is what the cart does afterwards.
    with capi_kernel_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(seeded_tenant.tenant_id)},
        )
        conn.execute(
            text(
                "UPDATE checkout_versions SET status = :s WHERE tenant_id = :t "
                "AND checkout_id = :c AND version = :v"
            ),
            {
                "s": "PAYMENT_FAILED",
                "t": seeded_tenant.tenant_id,
                "c": card["checkout_id"],
                "v": card["version"],
            },
        )

    written = auth_client.put(
        f"/v1/carts/{cart_id}/lines/{POPCORN}", json={"quantity": 1}, headers=_headers()
    )
    assert written.status_code == 200, written.text
    reopened = written.json()
    assert any(line["sku"] == POPCORN for line in reopened["lines"]), (
        "the buyer's cart came back and took the line they asked for"
    )
    assert reopened["quote"] is not None, "and came back priced, not merely writable"
