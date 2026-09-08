"""How long a sale took, and where that measurement is entitled to start.

A cart is not a transaction. Nothing in it is priced against a promise, no stock is held,
and a buyer who fills one and wanders off has cost the shop nothing. The transaction starts
at the opening version: the quote frozen, the hash minted, the units taken off the shelf.
It ends when the ``orders`` row is written from capture evidence. That span is the number
these tests are about.

**Razorpay cannot supply it.** Their clock starts at ``POST /v1/orders``, which this
platform sends only after the buyer has approved, so the provider can time a payment and
never a transaction. The half they cannot see -- pricing, the hold, the approval card, and
the person deciding -- is most of it.

**Both ends are stamped and subtracted by the database**, for the reason ``age_seconds``
already gives: the same clock stamped both rows, so an API pod with a skewed clock cannot
report a sale as faster than it was.

The figure is computed twice, because the order list is built with the query builder and
the order read is raw SQL. :func:`test_the_list_and_the_order_agree_on_the_same_sale` is
the guard that holds those two to one answer.

The total is also broken into four spans -- deciding, admitting, queued, paying -- because
a single number cannot tell "the shop was slow" from "the buyer was thinking", and those
are opposite findings. The spans are deliberately **not** a decomposition: any one of them
may be null on its own, and nothing makes them sum to the total. Attributing unexplained
time to the queue or to the buyer would invent the one thing this exists to establish.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from test_capi_listing import Confirmed, confirm_order, request_refund

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

#: Far enough that nothing incidental could produce it, and not a round minute, so a test
#: that passed by reading the wrong column would have to be wrong by exactly 95.
AGED_SECONDS = 95


def _age_the_opening_version(
    engine: Engine, tenant: SeededTenant, checkout_id: uuid.UUID, *, seconds: int, version: int = 1
) -> None:
    """Move one version's ``created_at`` back, so the span under test is a known number.

    The row is written by the database's own clock, so backdating it is the only way to
    produce a sale that took longer than the test does.
    """
    from sqlalchemy.orm import Session

    with Session(engine) as session, session.begin():
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenant.tenant_id)},
        )
        touched = session.execute(
            text(
                "UPDATE checkout_versions SET created_at = created_at - CAST(:s AS interval) "
                "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
            ),
            {"s": f"{seconds} seconds", "t": tenant.tenant_id, "c": checkout_id, "v": version},
        ).rowcount
    assert touched == 1, "the opening version must exist for this test to mean anything"


@pytest.fixture
def buyer(mint_client: Any) -> tuple[TestClient, MintedSession]:
    return mint_client(buyer_ref="duration")


@pytest.fixture
def order(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
) -> Confirmed:
    return confirm_order(capi_kernel_engine, seeded_tenant, buyer[1])


def test_a_sale_is_timed_from_the_kernels_first_freeze(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
    order: Confirmed,
) -> None:
    """The span begins where the platform first committed to something.

    Not at the cart, which is browsing, and not at the approval, which is already most of
    the way through. The buyer deciding is part of how long the sale took; a number that
    started after they pressed the button would be timing the machinery rather than the
    transaction.
    """
    client = buyer[0]
    _age_the_opening_version(
        capi_kernel_engine, seeded_tenant, order.checkout_id, seconds=AGED_SECONDS
    )

    read = client.get(f"/v1/orders/{order.order_id}")
    assert read.status_code == 200, read.text
    took = read.json()["duration_seconds"]
    assert took is not None, "a confirmed sale knows how long it took"
    assert AGED_SECONDS <= took <= AGED_SECONDS + 30, (
        f"measured from the opening version, so about {AGED_SECONDS}s, not {took}s"
    )


def test_the_list_and_the_order_agree_on_the_same_sale(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
    order: Confirmed,
) -> None:
    """One sale, two code paths, one answer.

    The list computes this against the query builder and the order read computes it in raw
    SQL, because those two modules were already written that way. Two expressions of one
    rule drift, and the drift would be invisible: both would keep returning a plausible
    number of seconds. This is the test that fails when they stop meaning the same thing.
    """
    client = buyer[0]
    _age_the_opening_version(
        capi_kernel_engine, seeded_tenant, order.checkout_id, seconds=AGED_SECONDS
    )

    detail = client.get(f"/v1/orders/{order.order_id}")
    assert detail.status_code == 200, detail.text
    listed = client.get("/v1/orders")
    assert listed.status_code == 200, listed.text

    rows = [row for row in listed.json()["orders"] if row["order_id"] == str(order.order_id)]
    assert len(rows) == 1, "the order appears once in its buyer's own list"
    assert rows[0]["duration_seconds"] == detail.json()["duration_seconds"]


def test_a_reopened_cart_is_still_timed_from_where_it_started(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
    order: Confirmed,
) -> None:
    """A buyer who changed their mind was still in this transaction the whole time.

    Reopening a cart ends the live version and opens the next one on the same checkout, so
    a sale can be approved on version 2 while it began on version 1. The list query already
    joins ``checkout_versions`` on the *approved* version to read its hash, and reusing that
    join here would silently restart the clock at the edit -- reporting a shopper who spent
    four minutes deciding as though they had taken twenty seconds.
    """
    from sqlalchemy.orm import Session

    client = buyer[0]
    _age_the_opening_version(
        capi_kernel_engine, seeded_tenant, order.checkout_id, seconds=AGED_SECONDS
    )

    # Version 2, minted now: the same document, so everything that resolves through the
    # approved version keeps resolving, and only the timestamp differs from version 1.
    with Session(capi_kernel_engine) as session, session.begin():
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(seeded_tenant.tenant_id)},
        )
        session.execute(
            text(
                "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                "version, content, content_hash, currency, total_minor, status, immutable, "
                "policy_receipt_id, policy_receipt_hash) "
                "SELECT gen_random_uuid(), tenant_id, merchant_id, checkout_id, 2, content, "
                "content_hash, currency, total_minor, status, immutable, policy_receipt_id, "
                "policy_receipt_hash FROM checkout_versions "
                "WHERE tenant_id = :t AND checkout_id = :c AND version = 1"
            ),
            {"t": seeded_tenant.tenant_id, "c": order.checkout_id},
        )
        session.execute(
            text("UPDATE orders SET checkout_version = 2 WHERE tenant_id = :t AND id = :o"),
            {"t": seeded_tenant.tenant_id, "o": order.order_id},
        )

    read = client.get(f"/v1/orders/{order.order_id}")
    assert read.status_code == 200, read.text
    took = read.json()["duration_seconds"]
    assert took is not None and took >= AGED_SECONDS, (
        f"the clock starts at version 1, not at the version that was approved; got {took}s"
    )


def test_an_order_whose_opening_version_is_unreadable_still_opens(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
    order: Confirmed,
) -> None:
    """A measurement must never delete the thing it was measuring.

    Both paths reach the opening version through an outer join for this reason. An inner
    one would drop the order from its own detail screen and from its buyer's list the
    moment the version could not be read -- turning a missing number into a missing sale.
    ``null`` says "not measured", which is the honest answer and one a surface can render.
    """
    from sqlalchemy.orm import Session

    client = buyer[0]
    with Session(capi_kernel_engine) as session, session.begin():
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(seeded_tenant.tenant_id)},
        )
        # Renumbered rather than deleted: `checkout_versions` is evidence and no role holds
        # DELETE on it. The effect under test is the same -- nothing answers for version 1.
        session.execute(
            text(
                "UPDATE checkout_versions SET version = 9 "
                "WHERE tenant_id = :t AND checkout_id = :c AND version = 1"
            ),
            {"t": seeded_tenant.tenant_id, "c": order.checkout_id},
        )
        session.execute(
            text("UPDATE orders SET checkout_version = 9 WHERE tenant_id = :t AND id = :o"),
            {"t": seeded_tenant.tenant_id, "o": order.order_id},
        )

    read = client.get(f"/v1/orders/{order.order_id}")
    assert read.status_code == 200, "the order still opens"
    assert read.json()["duration_seconds"] is None, "and says it was not measured"

    listed = client.get("/v1/orders")
    assert listed.status_code == 200, listed.text
    rows = [row for row in listed.json()["orders"] if row["order_id"] == str(order.order_id)]
    assert len(rows) == 1, "and is still in the list rather than dropped by its own join"
    assert rows[0]["duration_seconds"] is None


def test_the_spans_say_where_the_seconds_went(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
    order: Confirmed,
) -> None:
    """A total of ninety seconds means nothing until it says which ninety.

    "The shop was slow" and "the buyer was thinking" are opposite findings that a single
    figure reports identically, and a demonstration offering only the total invites the
    least flattering reading of it. Here the opening version is aged and the approval is
    not, so almost the whole span belongs to the person at the card -- and the assertion is
    that the platform says so rather than absorbing it into its own machinery.
    """
    client = buyer[0]
    _age_the_opening_version(
        capi_kernel_engine, seeded_tenant, order.checkout_id, seconds=AGED_SECONDS
    )

    read = client.get(f"/v1/orders/{order.order_id}")
    assert read.status_code == 200, read.text
    timing = read.json()["timing"]

    assert timing["deciding_seconds"] is not None, "the buyer's own time is reported"
    assert timing["deciding_seconds"] >= AGED_SECONDS, (
        "the aged span belongs to the buyer deciding, not to the platform"
    )
    # The machinery ran in this test's own lifetime, so whatever it took, it was not a
    # minute and a half. Asserted as a bound rather than a value: the point is that the
    # aged span landed on the buyer's side of the line and nowhere else.
    for name in ("admitting_seconds", "queued_seconds", "paying_seconds"):
        measured = timing[name]
        assert measured is None or measured < AGED_SECONDS, (
            f"{name} claimed time that belonged to the buyer"
        )


def test_a_span_nobody_can_measure_stays_null_rather_than_joining_its_neighbour(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
    order: Confirmed,
) -> None:
    """Unexplained time is reported as unexplained, not given to whoever is next in line.

    The spans are independent and are not made to add up. A missing milestone leaves its
    two spans null and the total unchanged, so the arithmetic visibly stops working -- and
    that is the honest outcome. Folding the gap into the neighbouring span would be the
    platform quietly deciding whether the buyer or the queue is to blame for seconds
    nothing recorded, which is exactly the question these numbers exist to answer.
    """
    from sqlalchemy.orm import Session

    client = buyer[0]
    _age_the_opening_version(
        capi_kernel_engine, seeded_tenant, order.checkout_id, seconds=AGED_SECONDS
    )

    with Session(capi_kernel_engine) as session, session.begin():
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(seeded_tenant.tenant_id)},
        )
        # Renumbered rather than deleted: approvals are evidence and no role holds DELETE.
        # Nothing now answers for the version this order names.
        moved = session.execute(
            text(
                "UPDATE approvals SET checkout_version = 7 "
                "WHERE tenant_id = :t AND checkout_id = :c"
            ),
            {"t": seeded_tenant.tenant_id, "c": order.checkout_id},
        ).rowcount
    assert moved >= 1, "there was an approval to displace"

    read = client.get(f"/v1/orders/{order.order_id}")
    assert read.status_code == 200, "the order still opens"
    body = read.json()
    assert body["duration_seconds"] is not None, "the total is still measurable end to end"
    assert body["timing"]["deciding_seconds"] is None, "and the span that lost an end says so"
    assert body["timing"]["admitting_seconds"] is None, "as does the span on its other side"
    assert body["timing"]["paying_seconds"] is not None, (
        "while a span with both ends intact is unaffected -- these are independent, not a "
        "chain that breaks at the first gap"
    )


def test_a_refund_grant_is_not_mistaken_for_the_one_that_authorised_the_sale(
    seeded_tenant: SeededTenant,
    buyer: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
    order: Confirmed,
) -> None:
    """Two kinds of authority hang off one payment attempt, and only one bought this order.

    A refund is authorised by an Execution Grant on the same attempt, issued long after the
    sale. Selecting grants by attempt alone would let a refund minted days later decide when
    this order was admitted, and the reported spans would move every time somebody asked for
    their money back. The operation is part of the question, not a detail.
    """
    client = buyer[0]
    _age_the_opening_version(
        capi_kernel_engine, seeded_tenant, order.checkout_id, seconds=AGED_SECONDS
    )
    before = client.get(f"/v1/orders/{order.order_id}")
    assert before.status_code == 200, before.text

    request_refund(client, order.order_id)

    after = client.get(f"/v1/orders/{order.order_id}")
    assert after.status_code == 200, after.text
    assert after.json()["timing"] == before.json()["timing"], (
        "a refund's own authority left the sale's timing exactly where it was"
    )
