"""The shop remembers what it charges and what it has, across a restart.

This is the whole point of moving merchant state out of the API process, and it is the one
property no other test in the suite could ever have caught. Every existing test runs inside
a single process, and the state used to live in that process -- so the state was always
there, and the day it would not be there was the day somebody deployed.

The failure this prevents, said plainly. Prices, stock, the fee policy and the running
offer lived in a dictionary in the API process (ADR 0003 D14). Every restart reseeded them
from the catalogue fixture. On Kubernetes the API rolls with ``Recreate``, so a deploy
silently returned the shop to its opening-day numbers -- mid-demonstration, with nothing in
the timeline saying it had happened, and with the reservation rows that had accumulated
against those numbers still in the database. The two halves of "how many are left" reset on
different schedules, and only one of them ever reset.

A second :func:`create_app` stands for the restart. It is a genuinely new registry with no
shared object of any kind, which is exactly what a second pod is; the only thing the two
apps have in common is the database. Every test here fails on the old code -- not by
raising, but by reading back the fixture's opening numbers instead of what was stored.
"""

from __future__ import annotations

from typing import Any

import pytest
from commerce_api.app import create_app
from commerce_api.settings import Settings
from commerce_domain import Money
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db.tenancy import set_tenant
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from conftest import MintedSession, merchant_mutation, merchant_store

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"


def _restart(settings: Settings) -> FastAPI:
    """A second app over the same database. What a redeploy leaves behind."""
    return create_app(settings)


def _product(app: FastAPI, session: MintedSession, sku: str) -> dict[str, Any]:
    with TestClient(app) as client:
        response = client.get(f"/v1/catalogue/products/{sku}", headers=session.auth_header)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


class TestARestartDoesNotResetTheShop:
    def test_an_injected_price_is_still_there_after_a_restart(
        self,
        api_app: FastAPI,
        demo_session: MintedSession,
        settings_for_tests: Settings,
    ) -> None:
        before = merchant_store(api_app, demo_session).get_product(MILK).unit_price.minor
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_price(MILK, Money(before + 4500, "INR"))

        restarted = _restart(settings_for_tests)

        assert _product(restarted, demo_session, MILK)["unit_price_minor"] == before + 4500

    def test_stock_a_demonstration_moved_is_still_moved(
        self,
        api_app: FastAPI,
        demo_session: MintedSession,
        settings_for_tests: Settings,
    ) -> None:
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_stock(MILK, 7)

        restarted = _restart(settings_for_tests)

        assert _product(restarted, demo_session, MILK)["stock_units"] == 7

    def test_the_revision_survives_so_a_stale_quote_stays_stale(
        self,
        api_app: FastAPI,
        demo_session: MintedSession,
        settings_for_tests: Settings,
    ) -> None:
        """The freshness token is state, and resetting it would un-stale a stale quote.

        A quote taken at revision N is stale once the shop reaches N+1, and that is what
        turns a price change into a fresh approval rather than a surprise. When the
        revision lived in memory a restart put it back to zero, so a quote taken before the
        restart compared equal to the shop after it and read as current.
        """
        before = merchant_store(api_app, demo_session).revision
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_price(MILK, Money(12345, "INR"))

        restarted = _restart(settings_for_tests)

        assert _product(restarted, demo_session, MILK)["freshness"]["catalogue_revision"] == (
            before + 1
        )

    def test_a_running_offer_is_still_running(
        self,
        api_app: FastAPI,
        demo_session: MintedSession,
        settings_for_tests: Settings,
        capi_app_engine: Engine,
    ) -> None:
        """An offer is a whole document, and it is stored as one."""
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.start_offer(
                offer_id="restart-offer",
                label="Ten percent",
                percent_bp=1000,
                effective_from_epoch_ms=1_700_000_000_000,
                effective_to_epoch_ms=1_900_000_000_000,
            )

        restarted = _restart(settings_for_tests)

        with Session(capi_app_engine) as db, db.begin():
            set_tenant(db, demo_session.tenant_id)
            promotion = restarted.state.merchants.store(db, demo_session.merchant_id).promotion
        assert promotion is not None
        assert promotion.offer_id == "restart-offer"
        assert promotion.percent_bp == 1000


class TestSeeding:
    def test_a_shop_is_opened_at_the_fixture_and_then_never_reopened(
        self,
        api_app: FastAPI,
        demo_session: MintedSession,
        settings_for_tests: Settings,
        capi_admin_engine: Engine,
    ) -> None:
        """Seeding runs once. A second run would restore fixture prices over a merchant's.

        The guard is a read rather than a grant, because the app role legitimately holds
        INSERT here -- opening a shop is provisioning. What it must not do is open one
        twice.
        """
        merchant_store(api_app, demo_session)  # first sight seeds the shop
        with merchant_mutation(api_app, demo_session) as scenario:
            scenario.set_price(MILK, Money(99900, "INR"))

        # Three more restarts, each of which would have reseeded under the old design.
        for _ in range(3):
            _restart(settings_for_tests)

        assert (
            _product(_restart(settings_for_tests), demo_session, MILK)["unit_price_minor"] == 99900
        )
        with capi_admin_engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": str(demo_session.tenant_id)},
            )
            rows = conn.execute(
                text("SELECT count(*) FROM merchant_state WHERE tenant_id = :t"),
                {"t": demo_session.tenant_id},
            ).scalar_one()
        assert rows == 1, "one shop, one row, however many times the API restarted"
