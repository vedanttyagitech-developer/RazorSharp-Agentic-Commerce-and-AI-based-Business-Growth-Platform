"""The merchant surface over HTTP, and its equivalence with the in-process backend.

Two kinds of test, and the split is the point.

The first kind serves a real :class:`merchant_sim.MerchantStore` over
``httpx.MockTransport`` in the wire shape the API actually emits, then asserts that
:class:`HttpBackend` and :class:`InMemoryBackend` return *the same objects* for the same
merchant. That is the claim :class:`MerchantBackend` exists to make: a merchant agent
tested against the simulator and run against the API must not answer two different
questions. Comparing whole dataclasses rather than field samples is deliberate -- a field
this test forgot to name is exactly the field a divergence would hide in.

The second kind talks to the live API on ``ACR_API_BASE`` (default ``127.0.0.1:8000``)
with a freshly minted demo session, and skips with a plain message when nothing answers.
Shapes get verified against the server rather than against the docstring that described
it, because a client written from a schema and never run is a client that agrees with the
schema and not the service.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from agent_runtime.backends import BackendError, HttpBackend, InMemoryBackend
from agent_runtime.backends.base import CheckoutMetrics, MerchantBackend
from agent_runtime.backends.memory import LOW_STOCK_UNITS
from agent_runtime.capabilities.proposals import ANOMALY_KINDS
from merchant_sim import MerchantStore, ScenarioController

#: Where the live API is expected. Overridable so the suite can be pointed at a staging
#: deployment without editing it; unreachable is a skip, never a failure.
LIVE_BASE: Final[str] = os.environ.get("ACR_API_BASE", "http://127.0.0.1:8000")

#: Long enough to notice a real answer, short enough that a suite run against nothing does
#: not stall on every test in this module.
LIVE_TIMEOUT_S: Final[float] = 5.0


# --------------------------------------------------------------- a fake of the real API


class _MerchantApi:
    """Serves one ``MerchantStore`` in the API's own wire shape.

    ``page_size`` is this fake's page, not the client's request: forcing a small page is
    how the walk, its bound and its cursor discipline get exercised without seeding a
    catalogue of thousands. The client's requested ``limit`` is recorded rather than
    honoured so a test can assert the client asks for the largest page the API allows.
    """

    def __init__(
        self,
        store: MerchantStore,
        *,
        page_size: int = 100,
        scope: str = "own",
        orders: list[dict[str, Any]] | None = None,
        order_counts: dict[str, int] | None = None,
        refund_counts: dict[str, int] | None = None,
        order_page_size: int = 100,
        stuck_cursor: bool = False,
        category_counts: dict[str, int] | None = None,
    ) -> None:
        self.store = store
        self.page_size = page_size
        self.scope = scope
        self.orders = orders or []
        self.order_counts = order_counts if order_counts is not None else {"CONFIRMED": 0}
        self.refund_counts = refund_counts if refund_counts is not None else {"REFUNDED": 0}
        self.order_page_size = order_page_size
        self.stuck_cursor = stuck_cursor
        self.category_counts = category_counts
        self.requests: list[httpx.Request] = []

    # -- catalogue

    def _row(self, sku: str) -> dict[str, Any]:
        view = self.store.get_product(sku)
        return {
            "sku": sku,
            "display_name": view.display_name(devanagari=False),
            "name_en": view.product.name_en,
            "name_hi": view.product.name_hi,
            "category": view.product.category.value,
            "unit_label": view.product.unit_label,
            "unit_price_minor": view.unit_price.minor,
            "unit_price": {"minor": view.unit_price.minor, "currency": "INR", "display": "0.00"},
            "currency": "INR",
            "tax_bp": view.product.tax_bp,
            "stock_units": view.stock_units,
            "is_listed": view.is_listed,
            "is_available": view.is_available,
            "freshness": {
                "source": view.freshness.source,
                "catalogue_revision": view.freshness.catalogue_revision,
                "observed_at": "2026-09-05T00:00:00Z",
            },
        }

    def _catalogue(self, request: httpx.Request) -> httpx.Response:
        skus = sorted(self.store.all_skus())
        cursor = request.url.params.get("cursor")
        start = skus.index(cursor) + 1 if cursor is not None else 0
        window = skus[start : start + self.page_size]
        after = start + len(window)
        by_category: dict[str, int] = {}
        for sku in skus:
            category = self.store.get_product(sku).product.category.value
            by_category[category] = by_category.get(category, 0) + 1
        next_cursor = window[-1] if window and after < len(skus) else None
        if self.stuck_cursor and cursor is not None:
            next_cursor = cursor
        return httpx.Response(
            200,
            json={
                "products": [self._row(sku) for sku in window],
                "next_cursor": next_cursor,
                "limit": self.page_size,
                "matched": len(skus),
                "counts_by_category": self.category_counts
                if self.category_counts is not None
                else by_category,
                "revision": self.store.revision,
            },
        )

    # -- collections

    def _orders(self, request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        start = int(cursor) if cursor is not None else 0
        window = self.orders[start : start + self.order_page_size]
        after = start + len(window)
        return httpx.Response(
            200,
            json={
                "orders": window,
                "next_cursor": str(after) if after < len(self.orders) else None,
                "limit": self.order_page_size,
                "scope": self.scope,
                "counts": self.order_counts,
            },
        )

    def _refunds(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "refunds": [],
                "next_cursor": None,
                "limit": 1,
                "scope": self.scope,
                "counts": self.refund_counts,
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        routes = {
            "/v1/catalogue/products": self._catalogue,
            "/v1/orders": self._orders,
            "/v1/refunds": self._refunds,
        }
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"type": "urn:acr:problem:no-route", "title": "no"})
        return route(request)

    def backend(self, *, scenario_key: str | None = None) -> HttpBackend:
        return HttpBackend(
            "https://api.test",
            bearer="session-token",
            scenario_key=scenario_key,
            transport=httpx.MockTransport(self.handler),
        )


def _order_row(
    amount_minor: int, *, refunded_minor: int = 0, currency: str = "INR"
) -> dict[str, Any]:
    return {"amount_minor": amount_minor, "currency": currency, "refunded_minor": refunded_minor}


@pytest.fixture
def troubled_store(store: MerchantStore, scenario: ScenarioController) -> MerchantStore:
    """A merchant with one of every anomaly, including both sides of the low-stock line.

    The boundary SKUs are the reason this fixture exists: a threshold tested only in the
    middle of its range passes just as happily when it is off by one.
    """
    skus = sorted(store.all_skus())
    scenario.sell_out(skus[0])
    scenario.set_stock(skus[1], 7)
    scenario.make_unavailable(skus[1])
    scenario.set_stock(skus[2], LOW_STOCK_UNITS)
    scenario.set_stock(skus[3], LOW_STOCK_UNITS + 1)
    scenario.make_unavailable(skus[4])
    scenario.set_stock(skus[4], 0)
    return store


# ------------------------------------------------------------------------- equivalence


@pytest.mark.asyncio
async def test_catalogue_health_is_the_same_answer_as_the_in_memory_backend(
    troubled_store: MerchantStore,
) -> None:
    api = _MerchantApi(troubled_store, page_size=40)
    async with api.backend() as http:
        assert (
            await http.catalogue_health()
            == await InMemoryBackend(troubled_store).catalogue_health()
        )


@pytest.mark.asyncio
async def test_inventory_anomalies_are_the_same_answer_as_the_in_memory_backend(
    troubled_store: MerchantStore,
) -> None:
    """Kinds, the low-stock threshold, the ordering and the limit, all at once."""
    api = _MerchantApi(troubled_store, page_size=40)
    memory = InMemoryBackend(troubled_store)
    async with api.backend() as http:
        assert await http.inventory_anomalies() == await memory.inventory_anomalies()
        assert await http.inventory_anomalies(limit=2) == await memory.inventory_anomalies(limit=2)

    anomalies = await memory.inventory_anomalies(limit=len(troubled_store.all_skus()))
    rank = {"listed_out_of_stock": 0, "delisted_with_stock": 1, "low_stock": 2}
    ranks = [rank[row.kind] for row in anomalies]
    assert {row.kind for row in anomalies} == ANOMALY_KINDS, "one of each was provoked"
    assert ranks == sorted(ranks), "empty shelves, then delisted stock, then low stock"
    at_threshold = {row.sku for row in anomalies if row.kind == "low_stock"}
    skus = sorted(troubled_store.all_skus())
    assert skus[2] in at_threshold, "a SKU exactly at the threshold is low on stock"
    assert skus[3] not in at_threshold, "one unit above the threshold is not"
    assert skus[4] not in {row.sku for row in anomalies}, "delisted and empty is not a problem"


@pytest.mark.asyncio
async def test_the_walk_asks_for_the_largest_page_and_follows_the_cursor(
    store: MerchantStore,
) -> None:
    api = _MerchantApi(store, page_size=40)
    async with api.backend() as http:
        health = await http.catalogue_health()
    assert health.listed + health.delisted == health.total, "every row was counted"
    assert {request.url.params["limit"] for request in api.requests} == {"100"}
    assert api.requests[0].url.params.get("cursor") is None
    assert api.requests[1].url.params["cursor"] == sorted(store.all_skus())[39]


@pytest.mark.asyncio
async def test_a_bounded_walk_returns_a_partial_count_that_says_it_is_partial(
    store: MerchantStore,
) -> None:
    """The bound stops the walk; ``total`` against the breakdown is how it says so.

    A count of the rows read, beside the catalogue size the API reported, is a statement a
    console can check with subtraction. A request that never returned would be worse, and
    so would a breakdown that silently claimed to span a catalogue it never finished.
    """
    api = _MerchantApi(store, page_size=1)
    async with api.backend() as http:
        health = await http.catalogue_health()
    assert len(api.requests) == 20, "the walk stops at its page bound"
    assert health.listed + health.delisted == 20
    assert health.total == len(store.all_skus()) > 20
    assert sum(health.by_category.values()) == health.total, "categories still span the whole"


@pytest.mark.asyncio
async def test_counts_by_category_is_taken_from_the_api_not_recomputed(
    store: MerchantStore,
) -> None:
    """The API's map spans the catalogue; a map rebuilt from a walked prefix would not."""
    api = _MerchantApi(store, page_size=1, category_counts={"dairy": 24, "electronics": 5})
    async with api.backend() as http:
        health = await http.catalogue_health()
    assert health.by_category == {"dairy": 24, "electronics": 5}


@pytest.mark.asyncio
async def test_the_reported_revision_is_the_lowest_the_walk_saw(
    store: MerchantStore, scenario: ScenarioController
) -> None:
    """A catalogue injected into mid-walk leaves the pages on different generations.

    The lowest revision seen is the only one the whole count is guaranteed to be no older
    than. Reporting the freshest instead would put a new label on a count whose first page
    was read before the change, and a console comparing the two would be reassured by it.
    """
    opening = store.revision
    sold_out = iter(sorted(store.all_skus(), reverse=True))

    class _Injecting(_MerchantApi):
        def _catalogue(self, request: httpx.Request) -> httpx.Response:
            response = super()._catalogue(request)
            scenario.sell_out(next(sold_out))
            return response

    moving = _Injecting(store, page_size=40)
    async with moving.backend() as http:
        health = await http.catalogue_health()
    assert len(moving.requests) > 1, "more than one page, so more than one revision"
    assert store.revision > opening, "the catalogue really did move under the walk"
    assert health.catalogue_revision == opening


@pytest.mark.asyncio
async def test_a_cursor_that_does_not_advance_is_refused(store: MerchantStore) -> None:
    api = _MerchantApi(store, page_size=40, stuck_cursor=True)
    async with api.backend() as http:
        with pytest.raises(BackendError) as excinfo:
            await http.catalogue_health()
    assert excinfo.value.problem.reason_key == "contract_violation"
    assert "did not advance" in excinfo.value.problem.detail
    assert len(api.requests) < 20, "the walk stopped on the fault, not on the bound"


@pytest.mark.asyncio
async def test_a_catalogue_row_missing_a_key_is_a_contract_error(store: MerchantStore) -> None:
    class _Broken(_MerchantApi):
        def _row(self, sku: str) -> dict[str, Any]:
            row = super()._row(sku)
            del row["stock_units"]
            return row

    async with _Broken(store).backend() as http:
        with pytest.raises(BackendError) as excinfo:
            await http.inventory_anomalies()
    assert excinfo.value.problem.status == 502
    assert "stock_units" in excinfo.value.problem.detail


# ---------------------------------------------------------------------------- metrics


@pytest.mark.asyncio
async def test_own_scope_never_reports_a_merchant_total(store: MerchantStore) -> None:
    """The caller's own orders under a heading that says "captured" is a dashboard lying."""
    api = _MerchantApi(
        store,
        scope="own",
        orders=[_order_row(4800, refunded_minor=500)],
        order_counts={"CONFIRMED": 1, "CANCELLED": 0},
        refund_counts={"REFUNDED": 1, "REFUND_PENDING": 0},
    )
    async with api.backend() as http:
        metrics = await http.checkout_metrics()
    assert metrics.captured_minor is None and metrics.refunded_minor is None
    assert metrics.orders_total == 1
    assert metrics.orders_by_state == {"CANCELLED": 0, "CONFIRMED": 1}
    assert metrics.refunds_by_state == {"REFUNDED": 1, "REFUND_PENDING": 0}


@pytest.mark.asyncio
async def test_tenant_scope_sums_the_integer_minor_units_the_server_sent(
    store: MerchantStore,
) -> None:
    api = _MerchantApi(
        store,
        scope="tenant",
        orders=[_order_row(4800, refunded_minor=500), _order_row(2200)],
        order_counts={"CONFIRMED": 2},
        order_page_size=1,
    )
    async with api.backend(scenario_key="k") as http:
        metrics = await http.checkout_metrics()
    assert metrics.captured_minor == 7000
    assert metrics.refunded_minor == 500
    assert metrics.currency == "INR"


@pytest.mark.asyncio
async def test_a_tenant_with_no_orders_has_captured_zero_not_nothing(
    store: MerchantStore,
) -> None:
    """Zero is a measurement here: the walk completed and found no orders.

    The scope is known and the walk is known to have finished, so "none" is a fact rather
    than an absence, and those are the two answers a merchant is owed apart. The in-memory
    backend answers the same, for the same reason -- it read all of its own state -- which
    is checked directly in ``test_ar_merchant_equivalence.py``.
    """
    api = _MerchantApi(store, scope="tenant", orders=[], order_counts={"CONFIRMED": 0})
    async with api.backend(scenario_key="k") as http:
        metrics = await http.checkout_metrics()
    assert metrics.captured_minor == 0 and metrics.refunded_minor == 0
    assert metrics.orders_total == 0


@pytest.mark.asyncio
async def test_a_truncated_order_walk_reports_no_money(store: MerchantStore) -> None:
    api = _MerchantApi(
        store,
        scope="tenant",
        orders=[_order_row(100) for _ in range(50)],
        order_counts={"CONFIRMED": 50},
        order_page_size=1,
    )
    async with api.backend(scenario_key="k") as http:
        metrics = await http.checkout_metrics()
    assert metrics.orders_total == 50, "the count spans the scope even when the walk did not"
    assert metrics.captured_minor is None, "a prefix summed is not a total"
    assert metrics.refunded_minor is None


@pytest.mark.asyncio
async def test_orders_in_two_currencies_have_no_single_total(store: MerchantStore) -> None:
    api = _MerchantApi(
        store,
        scope="tenant",
        orders=[_order_row(4800), _order_row(1200, currency="USD")],
        order_counts={"CONFIRMED": 2},
    )
    async with api.backend(scenario_key="k") as http:
        metrics = await http.checkout_metrics()
    assert metrics.captured_minor is None and metrics.refunded_minor is None


@pytest.mark.asyncio
async def test_an_unknown_scope_is_refused_rather_than_assumed_narrow(
    store: MerchantStore,
) -> None:
    api = _MerchantApi(store, scope="everything")
    async with api.backend() as http:
        with pytest.raises(BackendError) as excinfo:
            await http.checkout_metrics()
    assert excinfo.value.problem.reason_key == "contract_violation"
    assert "everything" in excinfo.value.problem.detail


@pytest.mark.asyncio
async def test_a_non_integer_count_is_refused(store: MerchantStore) -> None:
    """``counts`` is what separates "none" from "not measured"; a float would blur it."""
    api = _MerchantApi(store, order_counts={"CONFIRMED": 1.5})  # type: ignore[dict-item]
    async with api.backend() as http:
        with pytest.raises(BackendError) as excinfo:
            await http.checkout_metrics()
    assert excinfo.value.problem.reason_key == "contract_violation"
    assert "CONFIRMED" in excinfo.value.problem.detail


@pytest.mark.asyncio
async def test_the_scenario_key_rides_only_on_the_merchant_collections(
    store: MerchantStore,
) -> None:
    """It widens a collection to the tenant; on ``order_track`` it would widen ownership.

    An agent handed a merchant backend must not thereby gain the ability to open any
    buyer's order, so the key is attached per call rather than to the client.
    """
    api = _MerchantApi(store, scope="tenant")
    async with api.backend(scenario_key="operator-key") as http:
        await http.checkout_metrics()
        await http.catalogue_health()
    keyed = {request.url.path: request.headers.get("x-scenario-key") for request in api.requests}
    assert keyed["/v1/orders"] == "operator-key"
    assert keyed["/v1/refunds"] == "operator-key"
    assert keyed["/v1/catalogue/products"] is None


@pytest.mark.asyncio
async def test_without_a_scenario_key_no_header_is_sent_at_all(store: MerchantStore) -> None:
    api = _MerchantApi(store)
    async with api.backend() as http:
        await http.checkout_metrics()
    assert all("x-scenario-key" not in request.headers for request in api.requests)


@pytest.mark.asyncio
async def test_the_refund_page_is_fetched_for_its_counts_only(store: MerchantStore) -> None:
    api = _MerchantApi(store)
    async with api.backend() as http:
        await http.checkout_metrics()
    refund_calls = [r for r in api.requests if r.url.path == "/v1/refunds"]
    assert len(refund_calls) == 1
    assert refund_calls[0].url.params["limit"] == "1"


def test_the_http_backend_really_is_a_merchant_backend() -> None:
    """The tool factory builds the merchant tools off this check and nothing else."""
    assert issubclass(HttpBackend, MerchantBackend)


# --------------------------------------------------------------------------- live API


def _live_session() -> tuple[str, str] | None:
    """Mint a demo session, or ``None`` when nothing is listening."""
    try:
        response = httpx.post(
            f"{LIVE_BASE}/v1/demo/sessions",
            json={"tenant_slug": "demo", "actor_type": "BUYER"},
            timeout=LIVE_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    body = response.json()
    return str(body["token"]), str(body["tenant_id"])


@pytest.fixture(scope="module")
def live_token() -> str:
    session = _live_session()
    if session is None:
        pytest.skip(f"no API at {LIVE_BASE}; start it or set ACR_API_BASE to run the live tests")
    return session[0]


@pytest.fixture
def live(live_token: str) -> Iterator[HttpBackend]:
    yield HttpBackend(LIVE_BASE, bearer=live_token, timeout=LIVE_TIMEOUT_S)


@pytest.mark.asyncio
async def test_live_catalogue_health_counts_the_whole_seeded_catalogue(
    live: HttpBackend,
) -> None:
    async with live:
        health = await live.catalogue_health()
    assert health.total > 0
    assert health.listed + health.delisted == health.total, "the walk reached the end"
    assert health.available + health.out_of_stock <= health.total
    assert sum(health.by_category.values()) == health.total
    assert health.by_category == dict(sorted(health.by_category.items()))
    assert health.catalogue_revision >= 0


@pytest.mark.asyncio
async def test_live_inventory_anomalies_are_ordered_and_of_known_kinds(
    live: HttpBackend,
) -> None:
    async with live:
        anomalies = await live.inventory_anomalies(limit=50)
    assert {row.kind for row in anomalies} <= ANOMALY_KINDS
    rank = {"listed_out_of_stock": 0, "delisted_with_stock": 1, "low_stock": 2}
    keys = [(rank[row.kind], row.sku) for row in anomalies]
    assert keys == sorted(keys)
    assert all(row.name and isinstance(row.detail["stock_units"], int) for row in anomalies)
    # Every claim an anomaly makes is checkable from the row it was made about, so the
    # assertions stay inside one read. A second call to the live demo server could see a
    # catalogue an injection had moved in between, and a test that failed for that reason
    # would be reporting on the server's liveness rather than on this client.
    assert all(
        row.detail["stock_units"] == 0 for row in anomalies if row.kind == "listed_out_of_stock"
    )
    assert all(
        0 < row.detail["stock_units"] <= LOW_STOCK_UNITS
        for row in anomalies
        if row.kind == "low_stock"
    )


@pytest.mark.asyncio
async def test_live_checkout_metrics_refuse_to_call_an_own_scope_figure_a_total(
    live: HttpBackend,
) -> None:
    """A buyer session gets ``scope=own``, so the money fields must be absent, not zero."""
    async with live:
        metrics = await live.checkout_metrics()
    assert isinstance(metrics, CheckoutMetrics)
    assert metrics.captured_minor is None
    assert metrics.refunded_minor is None
    assert metrics.orders_by_state, "the API counts every order state, zeros included"
    assert metrics.refunds_by_state, "and every refund state"
    assert metrics.orders_total == sum(metrics.orders_by_state.values())
    assert metrics.currency == "INR"
