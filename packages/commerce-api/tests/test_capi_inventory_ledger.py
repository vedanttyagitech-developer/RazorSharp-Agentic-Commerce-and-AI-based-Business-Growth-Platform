"""A sold unit finally moves the stock number, and the ledger says what moved and why.

THE DEFECT THESE TESTS PIN
--------------------------
A sale did nothing to the shop's stock number. Nothing at all: ``stock_units`` was a figure
only a merchant could change, and the only trace of a sale was the reservation row that had
held the units. So the platform kept two half-answers to "how many are left" -- what the
merchant declared, which never fell, and what had been taken, which only rose -- and
subtracted them at check time.

That never oversold, so no money was ever wrong. It simply never settled. In the
development database it was measured: 136 units of one SKU sold against a declared 48, and
every checkout for it refused with stock on the shelf.

So the shelf is a balance now, and every unit that moves gets a row. The tests below hold
the three things that makes true: the balance is the sum of the movements and cannot drift
from them; a sale takes units off the shelf in the transaction that admits it; and a shop
can therefore keep selling, which is the whole user-visible point.

``test_a_shop_keeps_selling_past_its_opening_stock`` is the one to read first. It is the
bug, reproduced: sell more units than the shop was seeded with, and see it keep working
because the merchant restocked -- which under the old design was impossible, because the
sales accumulated forever against a number that never moved.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from commerce_api import inventory
from commerce_domain import Money
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db.tenancy import set_tenant
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from conftest import MintedSession, merchant_mutation, merchant_store

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": f"k-{uuid.uuid4().hex}", **extra}


def _buy(client: TestClient, *, quantity: int, sku: str = MILK) -> dict[str, Any]:
    """One whole purchase, admitted by the Kernel. Returns the decision."""
    cart = client.post("/v1/carts", headers=_headers())
    assert cart.status_code == 201, cart.text
    cart_id = cart.json()["cart_id"]
    line = client.put(
        f"/v1/carts/{cart_id}/lines/{sku}", json={"quantity": quantity}, headers=_headers()
    )
    assert line.status_code == 200, line.text
    opened = client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert opened.status_code == 201, opened.text
    card = opened.json()
    paid = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve-and-pay",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers=_headers(),
    )
    assert paid.status_code == 200, paid.text
    decision: dict[str, Any] = paid.json()
    assert decision["allowed"] is True, decision
    return decision


def _ledger(session: MintedSession, engine: Engine) -> tuple[dict[str, int], dict[str, int]]:
    """The balances and the same question answered by summing the movements."""
    with Session(engine) as db, db.begin():
        set_tenant(db, session.tenant_id)
        return (
            inventory.balances(db, tenant_id=session.tenant_id, merchant_id=session.merchant_id),
            inventory.recomputed_balances(
                db, tenant_id=session.tenant_id, merchant_id=session.merchant_id
            ),
        )


def _movements(engine: Engine, session: MintedSession, sku: str) -> list[Any]:
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(session.tenant_id)},
        )
        return list(
            conn.execute(
                text(
                    "SELECT kind, units, reason, checkout_id, merchant_action_id "
                    "FROM inventory_movements WHERE tenant_id = :t AND sku = :sku "
                    "ORDER BY occurred_at, id"
                ),
                {"t": session.tenant_id, "sku": sku},
            ).all()
        )


class TestTheBalanceIsTheLedger:
    def test_every_balance_equals_its_movements(
        self,
        auth_client: TestClient,
        api_app: FastAPI,
        demo_session: MintedSession,
        capi_app_engine: Engine,
    ) -> None:
        """The reason the balance column is allowed to exist.

        ``stock_units`` is a maintained balance, kept so that a quote does not pay for a
        SUM over a growing ledger. It is only honest while it cannot drift, and this is
        what makes that checkable rather than merely intended: every SKU, recomputed from
        the rows, after real movements of three different kinds.
        """
        _buy(auth_client, quantity=2)
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_stock(MILK, 40)

        balances, recomputed = _ledger(demo_session, capi_app_engine)

        assert balances, "the shop has no stock rows at all"
        for sku, held in balances.items():
            assert held == recomputed.get(sku, 0), f"{sku}: balance and ledger disagree"

    def test_a_sale_takes_the_units_off_the_shelf(
        self,
        auth_client: TestClient,
        api_app: FastAPI,
        demo_session: MintedSession,
        capi_app_engine: Engine,
    ) -> None:
        before = merchant_store(api_app, demo_session).check_inventory(MILK).available_units

        decision = _buy(auth_client, quantity=3)

        after = merchant_store(api_app, demo_session).check_inventory(MILK).available_units
        assert after == before - 3

        sold = [
            row for row in _movements(capi_app_engine, demo_session, MILK) if row.kind == "SOLD"
        ]
        assert len(sold) == 1
        assert sold[0].units == -3
        assert sold[0].reason == inventory.REASON_SALE_ADMITTED
        assert str(sold[0].checkout_id) == decision["checkout"]["checkout_id"]

    def test_the_shop_opens_with_movements_that_account_for_its_stock(
        self, api_app: FastAPI, demo_session: MintedSession, capi_app_engine: Engine
    ) -> None:
        """No opening balance appears from nowhere. The fixture's units are a delivery."""
        opening = merchant_store(api_app, demo_session).check_inventory(MILK).available_units
        rows = _movements(capi_app_engine, demo_session, MILK)

        assert [row.kind for row in rows] == ["RECEIVED"]
        assert rows[0].units == opening
        assert rows[0].reason == inventory.REASON_SHOP_OPENED


class TestTheGuardStoppedCountingSoldUnitsTwice:
    def test_a_sold_unit_is_subtracted_once_and_not_twice(
        self,
        auth_client: TestClient,
        api_app: FastAPI,
        demo_session: MintedSession,
    ) -> None:
        """The consumed hold no longer defends units the ledger has already removed.

        Before the ledger, a consumed hold had to keep standing guard because nothing else
        took the units off the shelf. Now the sale does, and counting both would make three
        units sold look like six.
        """
        before = merchant_store(api_app, demo_session).check_inventory(MILK).available_units
        _buy(auth_client, quantity=3)
        assert (
            merchant_store(api_app, demo_session).check_inventory(MILK).available_units
            == before - 3
        )

    def test_a_shop_keeps_selling_past_its_opening_stock(
        self,
        auth_client: TestClient,
        api_app: FastAPI,
        demo_session: MintedSession,
    ) -> None:
        """The bug, reproduced and then not happening.

        Sell the shop down to nothing, restock it, and sell again. Under the old design the
        sales accumulated against a figure that never moved, so the second half of this
        test was impossible: every checkout refused with stock on the shelf, and the only
        way out was to raise a number that would be reset by the next restart anyway.
        """
        opening = merchant_store(api_app, demo_session).check_inventory(MILK).available_units
        sold = 0
        while sold + 4 <= opening:
            _buy(auth_client, quantity=4)
            sold += 4
        assert (
            merchant_store(api_app, demo_session).check_inventory(MILK).available_units
            == opening - sold
        )

        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_stock(MILK, 30)

        _buy(auth_client, quantity=4)
        assert merchant_store(api_app, demo_session).check_inventory(MILK).available_units == 26


class TestAMerchantCommandReachesTheLedger:
    def test_a_merchant_adjustment_is_recorded_as_a_correction(
        self, api_app: FastAPI, demo_session: MintedSession, capi_app_engine: Engine
    ) -> None:
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_stock(MILK, 12)

        rows = _movements(capi_app_engine, demo_session, MILK)
        assert rows[-1].kind == "ADJUSTED"
        assert rows[-1].reason == inventory.REASON_MERCHANT_ADJUSTMENT

    def test_a_price_change_moves_no_units(
        self, api_app: FastAPI, demo_session: MintedSession, capi_app_engine: Engine
    ) -> None:
        """Only movements of units are movements. A price is not one."""
        # Read the shop first, which is what opens it: counting movements before the shop
        # exists would count its opening delivery as though a price change had caused it.
        merchant_store(api_app, demo_session)
        before = len(_movements(capi_app_engine, demo_session, MILK))
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_price(MILK, Money(9900, "INR"))
        assert len(_movements(capi_app_engine, demo_session, MILK)) == before
