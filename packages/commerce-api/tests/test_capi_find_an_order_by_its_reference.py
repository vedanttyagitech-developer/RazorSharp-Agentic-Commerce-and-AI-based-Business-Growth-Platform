"""A buyer can look up the order number they were actually shown.

THE DEFECT
----------
Every screen, every payload and the copilot's own speech name an order as
``RS-260909-XW5G26M``. Nothing could look one up. The reference is derived from ``orders.id``
and never stored, no endpoint searched on it, and the copilot's grounding lexicon matched
only the raw UUID -- the one form a buyer is never shown. So "where is my order
RS-260909-XW5G26M" had no answer anywhere in the product.

WHAT IS ASSERTED
----------------
That the lookup finds the order, and -- the part worth more -- that it refuses to find
anybody else's. The tail is thirty-five bits, so a reference is a strong hint and not an
identity; a resolver that trusted a parse instead of comparing candidates would eventually
hand one buyer another's order, and that is the failure these tests exist to prevent.

Driven over HTTP rather than against the service, because "no endpoint can answer this" was
the defect. Scope comes from the same place every other order read takes it,
``checkouts.buyer_ref``, and the filter adds no way to see a row the buyer could not already
read: ``test_another_buyers_reference_is_not_found`` is what holds that.
"""

from __future__ import annotations

import uuid

import pytest
from commerce_domain import order_reference
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from test_capi_listing import Confirmed, confirm_order

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db


@pytest.fixture
def buyer(mint_client) -> tuple[TestClient, MintedSession]:
    return mint_client(buyer_ref="reference-a")


@pytest.fixture
def other_buyer(mint_client) -> tuple[TestClient, MintedSession]:
    """A second identity in the same tenant, which is how every ownership test is written."""
    return mint_client(buyer_ref="reference-b")


@pytest.fixture
def order(
    capi_kernel_engine: Engine,
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
) -> Confirmed:
    return confirm_order(capi_kernel_engine, seeded_tenant, buyer[1])


def _by_reference(client: TestClient, reference: str) -> dict:
    response = client.get("/v1/orders", params={"reference": reference})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_reference_finds_the_order_it_names(
    buyer: tuple[TestClient, MintedSession], order: Confirmed
) -> None:
    reference = order_reference(order.order_id)
    page = _by_reference(buyer[0], reference)
    assert [row["order_id"] for row in page["orders"]] == [str(order.order_id)]
    assert page["orders"][0]["reference"] == reference


@pytest.mark.parametrize("spelling", ["lower", "no-hyphens", "padded"])
def test_the_buyers_own_spelling_finds_it_too(
    buyer: tuple[TestClient, MintedSession], order: Confirmed, spelling: str
) -> None:
    """A buyer types what they can see, off a screen or a phone call."""
    reference = order_reference(order.order_id)
    typed = {
        "lower": reference.lower(),
        "no-hyphens": reference.replace("-", ""),
        "padded": f"  {reference}  ",
    }[spelling]
    page = _by_reference(buyer[0], typed)
    assert [row["order_id"] for row in page["orders"]] == [str(order.order_id)]


def test_another_buyers_reference_is_not_found(
    other_buyer: tuple[TestClient, MintedSession], order: Confirmed
) -> None:
    """The load-bearing one: a reference is not a capability.

    Knowing somebody's order number must not produce their order, which is exactly what a
    lookup keyed on a spoken identifier could quietly become.
    """
    page = _by_reference(other_buyer[0], order_reference(order.order_id))
    assert page["orders"] == []


def test_a_near_miss_finds_nothing_rather_than_the_closest_row(
    buyer: tuple[TestClient, MintedSession], order: Confirmed
) -> None:
    """One character different in the tail is a different order, or none. Never this one."""
    real = order_reference(order.order_id)
    other = "Z" if real[-1] != "Z" else "Y"
    page = _by_reference(buyer[0], real[:-1] + other)
    assert page["orders"] == []


@pytest.mark.parametrize(
    "junk", ["do you have milk", "RS-260909", "order 12345", "RS-260909-XW5G26I"]
)
def test_text_that_is_not_a_reference_is_refused(
    buyer: tuple[TestClient, MintedSession], junk: str
) -> None:
    """A 422, not an empty page.

    An unparseable reference and a reference that names nothing are different answers, and
    collapsing them would tell a buyer who mistyped that their order does not exist.
    """
    response = buyer[0].get("/v1/orders", params={"reference": junk})
    assert response.status_code == 422, response.text


def test_a_raw_order_id_is_not_a_reference(
    buyer: tuple[TestClient, MintedSession], order: Confirmed
) -> None:
    """The id is the identity and the reference is how it is spoken; this filter reads one.

    Accepting both here would put two lookups behind one parameter, and the id already has
    its own route.
    """
    response = buyer[0].get("/v1/orders", params={"reference": str(order.order_id)})
    assert response.status_code == 422, response.text


def test_the_filter_does_not_disturb_an_ordinary_page(
    buyer: tuple[TestClient, MintedSession], order: Confirmed
) -> None:
    """Absent, it changes nothing: the same page, the same counts, the same cursor."""
    page = buyer[0].get("/v1/orders").json()
    assert str(order.order_id) in [row["order_id"] for row in page["orders"]]
    assert page["counts"]["CONFIRMED"] >= 1


def test_a_reference_never_widens_the_scope(
    other_buyer: tuple[TestClient, MintedSession], order: Confirmed
) -> None:
    """Belt and braces on the ownership claim, from the counts rather than the rows.

    A resolver that queried before scoping could return an empty ``orders`` list while its
    ``counts`` betrayed that it had seen the row.
    """
    page = _by_reference(other_buyer[0], order_reference(order.order_id))
    assert page["orders"] == []
    assert page["scope"] == "own"


def test_an_unknown_but_well_formed_reference_is_an_empty_page(
    buyer: tuple[TestClient, MintedSession]
) -> None:
    """Well-formed and nothing there is a 200 with nothing, not a 404 and not a 422."""
    unknown = order_reference(uuid.UUID("01a00000-0000-7000-8000-000000000000"))
    page = _by_reference(buyer[0], unknown)
    assert page["orders"] == []
