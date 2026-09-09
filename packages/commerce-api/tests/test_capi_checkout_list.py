"""The checkout collection: how a surface that kept nothing finds its checkout again.

The defect this file exists for. Opening a checkout closes the cart it was built from,
so ``GET /v1/carts/current`` -- which answers with the buyer's ``OPEN`` cart -- returns
``null`` from that moment on. The old buyer surface held the checkout id in component
state and persisted only the cart id, and dropped even that on hand-off; a reload
therefore had no identifier of any kind and no endpoint that would resolve one. Hundreds
of checkouts sat in ``APPROVAL_REQUIRED`` with nothing able to reach them.

``test_current_cart_is_empty_once_the_checkout_exists`` pins the first half of that and
passes on the code that had the bug: it is the *reason*, not the fix. Everything else
here is the fix, and every one of those tests is a 404 without the route.

What is asserted, beyond that it answers at all:

* a buyer's list is their own checkouts, and a second buyer in the same tenant sees none
  of them -- the same ownership fact (``checkouts.buyer_ref``) the single read uses, so
  the collection can never be a way around it;
* an AGENT session acting for the same buyer sees the same checkout, because it already
  may read any of them by id. That is the voice recovery path: a spoken session has no
  browser and therefore never had an identifier to keep;
* ``live`` is decided by the kernel's ``NON_TERMINAL_CHECKOUT_STATES`` and nothing else,
  so a cancelled checkout leaves the live list the moment it is cancelled;
* ``live`` together with a terminal ``state`` is refused rather than answered with an
  empty page, because an empty page would read as a statement about the buyer;
* counts span every state and ignore both filters, so "none of these" and "none at all"
  stay distinguishable.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from transaction_kernel import CheckoutState
from transaction_kernel.states import NON_TERMINAL_CHECKOUT_STATES

from conftest import MintedSession

pytestmark = pytest.mark.db

MILK: Final[str] = "AMUL-DAIRY-001"
PROBLEM: Final[str] = "application/problem+json"

MintClient = Callable[..., tuple[TestClient, MintedSession]]


# --------------------------------------------------------------------------- helpers


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}", **extra}


def _open_checkout(client: TestClient, *, quantity: int = 2) -> dict[str, Any]:
    """A cart with one priced line, checked out. Returns the card plus its cart id."""
    opened = client.post("/v1/carts", headers=_headers())
    assert opened.status_code == 201, opened.text
    cart_id = opened.json()["cart_id"]

    line = client.put(
        f"/v1/carts/{cart_id}/lines/{MILK}", json={"quantity": quantity}, headers=_headers()
    )
    assert line.status_code == 200, line.text

    checkout = client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert checkout.status_code == 201, checkout.text
    card: dict[str, Any] = checkout.json()
    card["cart_id"] = cart_id
    return card


def _list(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/v1/checkouts", params=params)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


# ------------------------------------------------------------------ the reason it exists


class TestTheCartCannotBeTheHandle:
    def test_current_cart_is_empty_once_the_checkout_exists(self, buyer_a: Any) -> None:
        """Passes on the buggy code too. It is what left the surface with nothing."""
        client, _ = buyer_a
        before = client.get("/v1/carts/current")
        assert before.status_code == 200, before.text

        card = _open_checkout(client)

        after = client.get("/v1/carts/current")
        assert after.status_code == 200, after.text
        # The cart that holds the checkout is CHECKED_OUT, and this endpoint answers only
        # for an OPEN one. Nothing here is broken; it is simply not a handle on a checkout.
        assert after.json()["cart"] is None
        assert card["checkout_id"]


# ------------------------------------------------------------------------ finding it again


class TestRecovery:
    def test_a_surface_that_kept_nothing_finds_its_checkout_again(self, buyer_a: Any) -> None:
        client, _ = buyer_a
        card = _open_checkout(client)

        # Everything a reload would have lost is dropped here on purpose: the only thing
        # carried into the request below is the session, which a cookie survives.
        page = _list(client, live=True)

        assert [row["checkout_id"] for row in page["checkouts"]] == [card["checkout_id"]]
        found = page["checkouts"][0]
        assert found["state"] == CheckoutState.APPROVAL_REQUIRED.value
        assert found["live"] is True
        assert found["cart_id"] == card["cart_id"]
        assert found["version"] == 1
        assert found["amount_minor"] == card["amount_minor"]
        assert found["content_hash"] == card["content_hash"]
        assert page["scope"] == "own"

    def test_the_agent_for_this_buyer_finds_the_same_checkout(
        self, buyer_a: Any, mint_client: MintClient
    ) -> None:
        """The voice path. A spoken session never held an identifier to lose."""
        client, session = buyer_a
        card = _open_checkout(client)

        agent, _ = mint_client(actor_type="AGENT", buyer_ref=session.buyer_ref)
        page = _list(agent, live=True)

        assert [row["checkout_id"] for row in page["checkouts"]] == [card["checkout_id"]]

    def test_a_buyer_does_not_see_another_buyers_checkout(self, buyer_a: Any, buyer_b: Any) -> None:
        client_a, _ = buyer_a
        card = _open_checkout(client_a)

        client_b, _ = buyer_b
        page = _list(client_b, live=True)

        assert page["checkouts"] == []
        assert card["checkout_id"] not in [row["checkout_id"] for row in page["checkouts"]]
        # And the single read agrees, which is the point: the collection is not a way past it.
        assert client_b.get(f"/v1/checkouts/{card['checkout_id']}").status_code == 404


# ------------------------------------------------------------------------------- filters


class TestLiveness:
    def test_a_cancelled_checkout_leaves_the_live_list(self, buyer_a: Any) -> None:
        client, _ = buyer_a
        card = _open_checkout(client)
        assert len(_list(client, live=True)["checkouts"]) == 1

        cancelled = client.post(
            f"/v1/checkouts/{card['checkout_id']}/cancel",
            json={"reason": "buyer_cancelled"},
            headers=_headers(),
        )
        assert cancelled.status_code == 200, cancelled.text

        assert _list(client, live=True)["checkouts"] == []
        # Still listed unfiltered, and still the buyer's. Gone from "live", not from view.
        everything = _list(client)
        assert [row["checkout_id"] for row in everything["checkouts"]] == [card["checkout_id"]]
        assert everything["checkouts"][0]["live"] is False
        assert everything["checkouts"][0]["state"] == CheckoutState.CANCELLED.value

    def test_live_is_the_kernels_set_and_not_a_second_copy(self, buyer_a: Any) -> None:
        """Whatever the kernel calls non-terminal is what ``live=true`` returns."""
        client, _ = buyer_a
        _open_checkout(client)
        page = _list(client, live=True)
        for row in page["checkouts"]:
            assert CheckoutState(row["state"]) in NON_TERMINAL_CHECKOUT_STATES

    def test_live_with_a_terminal_state_is_refused_rather_than_answered_empty(
        self, buyer_a: Any
    ) -> None:
        client, _ = buyer_a
        _open_checkout(client)

        refused = client.get("/v1/checkouts", params={"live": True, "state": "CANCELLED"})

        assert refused.status_code == 422
        assert refused.headers["content-type"].startswith(PROBLEM)
        assert refused.json()["state"] == "CANCELLED"


class TestCounts:
    def test_counts_span_every_state_and_ignore_the_filter(self, buyer_a: Any) -> None:
        client, _ = buyer_a
        _open_checkout(client)

        page = _list(client, state="CANCELLED")

        assert page["checkouts"] == []
        # The filter returned nothing; the counts still say what the buyer has.
        assert page["counts"][CheckoutState.APPROVAL_REQUIRED.value] == 1
        assert page["counts"][CheckoutState.CANCELLED.value] == 0
        assert set(page["counts"]) == {member.value for member in CheckoutState}


class TestPaging:
    def test_a_keyset_walk_visits_every_checkout_once(self, buyer_a: Any) -> None:
        client, _ = buyer_a
        opened = [_open_checkout(client)["checkout_id"] for _ in range(3)]

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(len(opened) + 1):
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            page = _list(client, **params)
            seen.extend(row["checkout_id"] for row in page["checkouts"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

        assert cursor is None
        assert seen == list(reversed(opened))
        assert len(set(seen)) == len(seen)

    def test_a_malformed_cursor_is_a_400_problem(self, buyer_a: Any) -> None:
        client, _ = buyer_a
        refused = client.get("/v1/checkouts", params={"cursor": "not-a-cursor"})
        assert refused.status_code == 400
        assert refused.headers["content-type"].startswith(PROBLEM)


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture
def buyer_a(mint_client: MintClient) -> tuple[TestClient, MintedSession]:
    return mint_client(buyer_ref="list-a")


@pytest.fixture
def buyer_b(mint_client: MintClient) -> tuple[TestClient, MintedSession]:
    return mint_client(buyer_ref="list-b")
