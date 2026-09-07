"""The date under a line needs a merchant behind it, and this is where it crosses the wire.

No surface draws a delivery date today, and the reason is that nothing sends one. The last
one that tried printed "8 MINS" on every product card from no source at all, and the fix
was to delete the pill rather than to source it (``product-card.tsx`` records why). The two
ways to put the sentence back without a field would both have been the same mistake again:
compute a date in the API, which is a number with no merchant behind it, or let the surface
guess, which is that one layer further out. So merchant-sim -- the merchant -- declares a
promise in whole days per product, and this suite pins what the API does with it.

The split it enforces: the server sends days, the surface adds them to today. That is not
squeamishness. A date is the merchant's promise resolved against the buyer's calendar day
in the buyer's timezone, and this API knows neither; and a date next to a quote would be a
value that changes at midnight beside figures whose whole point is that two identical
carts priced a second apart are the same checkout.

The last test is the one that matters most. An approval card's breakdown is rebuilt from
the immutable, hashed document the buyer's consent binds to, and that document does not
carry a delivery promise -- so the card says ``null``, meaning "the approved bytes do not
state this", and never ``0``, which would be the card promising same-day delivery on its
own authority.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from merchant_sim import PRODUCTS_BY_SKU

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"  # dairy: off the dark store's shelves, same day
PHONE = "APPL-ELEC-001"  # electronics: from a warehouse, two days


def _headers() -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}


def _basket_with(client: TestClient, *lines: tuple[str, int]) -> dict[str, Any]:
    created = client.post("/v1/carts", headers=_headers())
    assert created.status_code == 201, created.text
    cart_id = created.json()["cart_id"]
    body: dict[str, Any] = created.json()
    for sku, quantity in lines:
        response = client.put(
            f"/v1/carts/{cart_id}/lines/{sku}",
            json={"quantity": quantity},
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
    return body


def test_a_product_read_states_the_merchant_s_promise(auth_client: TestClient) -> None:
    """The value is the merchant's, asserted by identity against the catalogue record.

    Compared against ``PRODUCTS_BY_SKU`` rather than against a literal, because the thing
    worth proving is that the API repeats what the merchant declared. A literal here would
    still pass if the serialiser had started making the number up.
    """
    for sku in (MILK, PHONE):
        response = auth_client.get(f"/v1/catalogue/products/{sku}")
        assert response.status_code == 200, response.text
        assert (
            response.json()["delivery_promise_days"] == PRODUCTS_BY_SKU[sku].delivery_promise_days
        )

    # And the two are genuinely different, so a serialiser that sent one constant fails.
    same_day = auth_client.get(f"/v1/catalogue/products/{MILK}").json()
    later = auth_client.get(f"/v1/catalogue/products/{PHONE}").json()
    assert same_day["delivery_promise_days"] == 0
    assert later["delivery_promise_days"] == 2


def test_every_product_on_a_catalogue_page_states_one(auth_client: TestClient) -> None:
    """A page with the field missing on one row is a row the storefront cannot render."""
    response = auth_client.get("/v1/catalogue/products", params={"limit": 100})
    assert response.status_code == 200, response.text
    products = response.json()["products"]
    assert products
    for product in products:
        assert product["delivery_promise_days"] == (
            PRODUCTS_BY_SKU[product["sku"]].delivery_promise_days
        ), product["sku"]


def test_a_search_hit_states_one(auth_client: TestClient) -> None:
    """Discovery is where a buyer first sees the promise, beside the price and the stock."""
    response = auth_client.get("/v1/catalogue/search", params={"q": "iphone", "limit": 5})
    assert response.status_code == 200, response.text
    hits = response.json()["hits"]
    assert hits, response.text
    for hit in hits:
        assert hit["delivery_promise_days"] == PRODUCTS_BY_SKU[hit["sku"]].delivery_promise_days


def test_a_quoted_line_states_one_so_the_cart_can_draw_a_date_per_line(
    auth_client: TestClient,
) -> None:
    """Per line, not per cart: a cart holding milk and a phone promises two dates.

    This is why the promise travels on the quote line rather than being looked up again
    per SKU by whoever is drawing the cart. One read, and every line already knows.
    """
    body = _basket_with(auth_client, (MILK, 2), (PHONE, 1))
    assert body["quote"] is not None, body
    promises = {line["sku"]: line["delivery_promise_days"] for line in body["quote"]["lines"]}
    assert promises == {MILK: 0, PHONE: 2}


def test_the_promise_survives_a_re_read_of_the_basket(auth_client: TestClient) -> None:
    """The cart is re-quoted on every read, so the field has to come back every time."""
    body = _basket_with(auth_client, (PHONE, 1))
    cart_id = body["cart_id"]
    again = auth_client.get(f"/v1/carts/{cart_id}")
    assert again.status_code == 200, again.text
    assert again.json()["quote"]["lines"][0]["delivery_promise_days"] == 2


def test_an_approval_card_says_not_stated_rather_than_same_day(auth_client: TestClient) -> None:
    """The card is rebuilt from the bytes the buyer's approval binds to, and they omit it.

    ``null`` is the honest answer: the approved document does not state a delivery promise,
    because putting one in it would change the hash of every checkout this store can
    produce and make a merchant moving a promise by a day invalidate approvals for orders
    whose price, contents and total had not moved at all.

    ``0`` would be the failure this test exists to catch -- a consent screen promising
    same-day delivery on its own authority, next to a total it is asking a human to agree
    to. A card must show nothing there rather than promise today.
    """
    body = _basket_with(auth_client, (PHONE, 1))
    opened = auth_client.post(f"/v1/carts/{body['cart_id']}/checkout", headers=_headers())
    assert opened.status_code == 201, opened.text
    card = opened.json()

    assert card["quote"] is not None, card
    assert [line["delivery_promise_days"] for line in card["quote"]["lines"]] == [None]
    # The rate beside it is null for the same reason and always has been; asserted here so
    # the two stay one decision rather than drifting into two.
    assert [line["tax_bp"] for line in card["quote"]["lines"]] == [None]
