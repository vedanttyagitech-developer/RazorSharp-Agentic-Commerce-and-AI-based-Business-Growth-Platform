"""The two backends answer the same question, and neither draws a zero it did not count.

Three claims are checked here, and each is a failure mode rather than a behaviour that
happened to be observable.

*One protocol, one answer.* :class:`MerchantBackend` exists so a merchant agent developed
against the simulator behaves the same when it is handed the API. That is only true if
somebody checks, and the check has to be the *same* read run against both -- a suite where
each backend is tested against its own expectations passes happily while the two disagree.
:func:`run_the_contract` is therefore written once and run against every backend, and
``test_the_two_backends_return_identical_records_for_one_merchant`` compares whole
dataclasses over one shared merchant state.

*Absent is not zero, all the way to the pixel.* The card is the last inch and the
persuasive one: a merchant reads a number on a card as a fact about their shop. So the
sweep below walks every row of every metrics card and refuses to find a figure the
platform did not derive rendered as anything but "not measured" -- including the case that
motivated it, a catalogue too large for the bounded walk whose unread pages would
otherwise report an uncounted shelf as a confident zero.

*A buyer principal is never offered a merchant tool.* ``InMemoryBackend`` implements the
merchant surface, so a shopping toolset built over it is assembled by a factory that has
every merchant builder to hand. What keeps them away is the roster and the capability
check, not the absence of the data, which is why the guarantee is worth pinning from the
buyer's side and not only from the merchant's.

The live half talks to the API on ``ACR_API_BASE`` and skips when nothing answers. It
exists because a client written from a schema and never run agrees with the schema rather
than with the service -- which is exactly how the buyer-surface parsers came to read a
flat ``source`` that no catalogue route has ever sent.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from agent_runtime.backends import HttpBackend, InMemoryBackend
from agent_runtime.backends.base import (
    CatalogueHealth,
    CheckoutMetrics,
    CommerceBackend,
    InventoryAnomaly,
    Locale,
    MerchantBackend,
)
from agent_runtime.capabilities import (
    ALL_CAPABILITIES,
    SPECIALIST_TOOLS,
    AgentRole,
    BoundToolset,
    build_toolset,
    derive_principal,
)
from agent_runtime.capabilities.tools import MERCHANT_METRICS, METRIC_CATALOGUE_HEALTH
from agent_runtime.language import Language
from agent_runtime.rendering.cards import NOT_MEASURED
from agent_runtime.turn import TurnContext
from merchant_sim import MerchantStore, ScenarioController
from transaction_kernel import ActorType, AgentPrincipal

from .conftest import MILK_SKU

#: Where the live API is expected. Unreachable is a skip, never a failure: the suite has
#: to run on a laptop with nothing else started.
LIVE_BASE: Final[str] = os.environ.get("ACR_API_BASE", "http://127.0.0.1:8000")
LIVE_TIMEOUT_S: Final[float] = 5.0

#: The closed vocabulary an anomaly's ``kind`` may take, and its urgency order. Both are
#: written out rather than imported: a test that imported the ordering it checks would
#: agree with any ordering at all, and a vocabulary that can grow without anyone editing
#: this line is not closed.
ANOMALY_RANK: Final[dict[str, int]] = {
    "listed_out_of_stock": 0,
    "delisted_with_stock": 1,
    "low_stock": 2,
}


class Closing:
    """``async with`` over a backend that owns an HTTP client, so no test leaks one."""

    def __init__(self, backend: HttpBackend) -> None:
        self._backend = backend

    async def __aenter__(self) -> HttpBackend:
        return self._backend

    async def __aexit__(self, *_: object) -> None:
        await self._backend.aclose()


# ------------------------------------------------------- the contract, written once


def check_catalogue_health(health: CatalogueHealth) -> None:
    """What must hold of a catalogue count from any backend, complete walk or not."""
    for figure in (health.total, health.listed, health.delisted, health.available):
        assert isinstance(figure, int) and not isinstance(figure, bool)
        assert figure >= 0
    assert isinstance(health.out_of_stock, int) and health.out_of_stock >= 0
    # Every product is listed or delisted and never both, so the pair counts the rows the
    # breakdown actually saw. It may fall short of ``total`` on a bounded walk; it may
    # never exceed it, because that would mean a row was counted twice.
    assert health.listed + health.delisted <= health.total
    # Availability is listing and stock together, so a listed product is available or out
    # of stock and a delisted one is neither. Equality here is what keeps "sold out" and
    # "delisted" from collapsing into each other somewhere in a backend's loop.
    assert health.available + health.out_of_stock == health.listed
    # ``by_category`` is reported across the catalogue rather than across the walk, so it
    # is the one breakdown that must add up to the whole even when the rest cannot.
    assert sum(health.by_category.values()) == health.total
    assert all(count >= 0 for count in health.by_category.values())
    assert health.catalogue_revision >= 0


def check_anomalies(anomalies: tuple[InventoryAnomaly, ...], *, limit: int) -> None:
    """Closed vocabulary, detail that agrees with the word, and the urgent ones first."""
    assert len(anomalies) <= limit
    for anomaly in anomalies:
        assert anomaly.kind in ANOMALY_RANK
        assert anomaly.sku and anomaly.name
        stock = anomaly.detail.get("stock_units")
        assert isinstance(stock, int) and not isinstance(stock, bool)
        # An observation that contradicts its own label is worse than no observation: the
        # console renders the label, and a merchant acts on it.
        if anomaly.kind == "listed_out_of_stock":
            assert stock == 0
        else:
            assert stock > 0
    keys = [(ANOMALY_RANK[a.kind], a.sku) for a in anomalies]
    assert keys == sorted(keys), "an empty shelf outranks a low-stock note, whatever the page"


def check_checkout_metrics(metrics: CheckoutMetrics) -> None:
    """Counts are integers; money is an integer of minor units, or nothing at all."""
    assert isinstance(metrics.orders_total, int) and not isinstance(metrics.orders_total, bool)
    assert metrics.orders_total >= 0
    for breakdown in (metrics.orders_by_state, metrics.refunds_by_state):
        for state, count in breakdown.items():
            assert isinstance(state, str) and state
            assert isinstance(count, int) and not isinstance(count, bool) and count >= 0
    for amount in (metrics.captured_minor, metrics.refunded_minor):
        # ``float`` is the failure this line exists for. An amount that arrived as 1234.56
        # would render, would look right, and would be wrong by rounding forever after.
        assert amount is None or (isinstance(amount, int) and not isinstance(amount, bool))
        assert amount is None or amount >= 0
    # The breakdown counts orders, so it accounts for all of them and no more. A backend
    # that filled this with checkout statuses would report six of something on a card row
    # reading "Orders in ..." beside a total of one, and nothing on the card would say
    # which number to believe.
    assert sum(metrics.orders_by_state.values()) == metrics.orders_total
    assert len(metrics.currency) == 3 and metrics.currency.isupper()


async def run_the_contract(backend: MerchantBackend) -> None:
    """Every invariant against one backend. The point is that it takes any of them."""
    check_catalogue_health(await backend.catalogue_health())
    check_anomalies(await backend.inventory_anomalies(5), limit=5)
    check_checkout_metrics(await backend.checkout_metrics())


# ------------------------------------------------------------------ a fake of the API


class CatalogueApi:
    """Serves one ``MerchantStore`` in the wire shape the catalogue routes actually emit.

    ``page_size`` is this fake's page rather than the client's request, which is how a
    walk long enough to hit its own bound gets exercised without seeding thousands of
    SKUs. The order and refund collections answer with fixed counts, because what varies
    in this file is the catalogue.
    """

    def __init__(
        self,
        store: MerchantStore,
        *,
        page_size: int = 100,
        scope: str = "tenant",
        orders: list[dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.page_size = page_size
        self.scope = scope
        self.orders = orders if orders is not None else []

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
        return httpx.Response(
            200,
            json={
                "products": [self._row(sku) for sku in window],
                "next_cursor": window[-1] if window and after < len(skus) else None,
                "limit": self.page_size,
                "matched": len(skus),
                "counts_by_category": by_category,
                "revision": self.store.revision,
            },
        )

    def _collection(self, key: str, rows: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                key: rows,
                "next_cursor": None,
                "limit": 100,
                "scope": self.scope,
                "counts": {"CONFIRMED": len(rows)} if key == "orders" else {},
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalogue/products":
            return self._catalogue(request)
        if request.url.path == "/v1/orders":
            return self._collection("orders", self.orders)
        if request.url.path == "/v1/refunds":
            return self._collection("refunds", [])
        return httpx.Response(404, json={"type": "urn:acr:problem:no-route", "title": "no"})

    def backend(self) -> HttpBackend:
        return HttpBackend(
            "https://api.test",
            bearer="session-token",
            scenario_key="operator-key",
            transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture
def messy_store(store: MerchantStore, scenario: ScenarioController) -> MerchantStore:
    """A merchant with something wrong in every direction, so the counts have work to do.

    One of each anomaly kind, plus a delisted-and-empty SKU that is none of them: a
    product nobody can buy and nobody is selling is not a problem to chase, and a backend
    that reported it would fill a merchant's list with rows they cannot act on.
    """
    skus = sorted(store.all_skus())
    scenario.set_stock(skus[0], 0)
    scenario.set_availability(skus[1], False)
    scenario.set_stock(skus[2], 1)
    scenario.set_stock(skus[3], 0)
    scenario.set_availability(skus[3], False)
    return store


# ------------------------------------------- 1. the two backends answer the same thing


@pytest.mark.asyncio
async def test_the_two_backends_return_identical_records_for_one_merchant(
    messy_store: MerchantStore,
) -> None:
    """The same merchant, read two ways, compared as whole dataclasses.

    Whole records rather than sampled fields for the two catalogue reads: a field this
    test forgot to name is exactly where a divergence would sit, and the equality operator
    does not forget one.

    ``checkout_metrics`` is compared field by field because one field is *entitled* to
    differ. The in-memory backend keeps no refund ledger, so ``refunded_minor`` is genuinely
    absent there while the API has summed settled refunds and can report zero. Asserting
    whole-record equality would force one of those two to lie, and the one that would give
    is the honest ``None``. Every other field must match, and they are named individually
    so a new field on :class:`CheckoutMetrics` fails this test until somebody decides
    which of the two it belongs to.
    """
    memory = InMemoryBackend(messy_store)
    async with Closing(CatalogueApi(messy_store).backend()) as http:
        assert await http.catalogue_health() == await memory.catalogue_health()
        assert await http.inventory_anomalies(20) == await memory.inventory_anomalies(20)
        over_api = await http.checkout_metrics()
        in_memory = await memory.checkout_metrics()
    assert over_api.orders_total == in_memory.orders_total
    assert over_api.orders_by_state == in_memory.orders_by_state
    assert over_api.captured_minor == in_memory.captured_minor
    assert over_api.currency == in_memory.currency
    assert in_memory.refunded_minor is None and over_api.refunded_minor == 0
    assert set(vars(CheckoutMetrics)["__slots__"]) == {
        "orders_total",
        "orders_by_state",
        "refunds_by_state",
        "captured_minor",
        "refunded_minor",
        "currency",
    }


@pytest.mark.asyncio
async def test_an_unsold_merchant_reports_a_counted_zero_from_either_backend(
    store: MerchantStore,
) -> None:
    """Nothing sold yet, and both backends say zero captured rather than "not measured".

    This is the divergence the protocol exists to prevent, in its sharpest form. Both
    backends know their scope and know they read all of it, so zero is what they counted.
    A backend that answered ``None`` here would move a merchant agent from "you have made
    no sales today" to "I could not check", on nothing but which one it was handed.
    """
    memory = InMemoryBackend(store)
    async with Closing(CatalogueApi(store).backend()) as http:
        assert (await http.checkout_metrics()).captured_minor == 0
    assert (await memory.checkout_metrics()).captured_minor == 0
    # The refund ledger is the real absence in the in-memory backend, and it says so
    # rather than borrowing the zero from the sum beside it.
    assert (await memory.checkout_metrics()).refunded_minor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("over_http", [False, True], ids=["memory", "http"])
async def test_every_backend_satisfies_the_same_merchant_contract(
    messy_store: MerchantStore, over_http: bool
) -> None:
    """One invariant suite, both implementations. Neither gets its own definition of true."""
    if not over_http:
        await run_the_contract(InMemoryBackend(messy_store))
        return
    async with Closing(CatalogueApi(messy_store).backend()) as http:
        await run_the_contract(http)


# -------------------------------------------------- 2. absent is not zero, on the card


def _harness() -> AgentPrincipal:
    return AgentPrincipal(
        principal_id="agent:merchant-copilot",
        tenant_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        actor_type=ActorType.AGENT,
        agent_role=None,
        capabilities=ALL_CAPABILITIES,
    )


def _toolset(role: AgentRole, backend: CommerceBackend) -> BoundToolset:
    specialist = derive_principal(_harness(), role)
    turn = TurnContext(language=Language.EN, principal=specialist, max_tool_calls=12)
    return build_toolset(role, backend, turn, principal=specialist, session_id="merchant-1")


class _Ctx:
    """What ADK's ToolContext looks like to a tool: a state mapping and a call id."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.function_call_id: str | None = "call-1"


async def _card_rows(backend: CommerceBackend, metric: str) -> list[dict[str, Any]]:
    """Read a metric and then draw it, the way the runtime would, and hand back the rows."""
    toolset = _toolset(AgentRole.GROWTH, backend)
    ctx = _Ctx()
    read = await toolset.get(f"{metric}_read").func(tool_context=ctx)
    assert read["ok"] is True
    card = await toolset.get("present_metrics").func(metric=metric, tool_context=ctx)
    assert card["ok"] is True
    items: list[dict[str, Any]] = card["card"]["items"]
    return items


def _assert_honest(rows: list[dict[str, Any]]) -> None:
    """A row is measured with a figure, or unmeasured with none. Never anything between.

    Both directions matter. A row carrying ``measured: False`` beside a number would let a
    surface that trusts the number render it anyway; a row carrying ``measured: True``
    beside no number would print "not measured" under a flag saying it had been counted.
    """
    for row in rows:
        figure = row["value_minor"] if row["value_minor"] is not None else row["count"]
        if row["measured"]:
            assert figure is not None, row
            assert isinstance(figure, int) and not isinstance(figure, bool), row
            assert row["display"] != NOT_MEASURED, row
        else:
            assert row["value_minor"] is None and row["count"] is None, row
            assert row["display"] == NOT_MEASURED, row


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", MERCHANT_METRICS)
async def test_no_metrics_card_row_ever_disagrees_with_its_own_measured_flag(
    messy_store: MerchantStore, metric: str
) -> None:
    _assert_honest(await _card_rows(InMemoryBackend(messy_store), metric))
    async with Closing(CatalogueApi(messy_store).backend()) as http:
        _assert_honest(await _card_rows(http, metric))


@pytest.mark.asyncio
async def test_a_refund_total_nobody_summed_is_drawn_as_not_measured(
    store: MerchantStore,
) -> None:
    """The row most tempting to fill in, on a backend that cannot fill it.

    "Refunded: 0.00" and "Refunded: not measured" send a merchant to two different
    afternoons, and the in-memory backend keeps no refund ledger, so only one of them is
    true. Captured on the same card is a counted zero, which makes this a test of the
    distinction rather than of a blanket refusal to print zeros.
    """
    drawn = await _card_rows(InMemoryBackend(store), "checkout_metrics")
    rows = {row["label"]: row for row in drawn}
    assert rows["Refunded"]["measured"] is False
    assert rows["Refunded"]["value_minor"] is None
    assert rows["Refunded"]["display"] == NOT_MEASURED
    assert rows["Captured"]["measured"] is True
    assert rows["Captured"]["value_minor"] == 0


@pytest.mark.asyncio
async def test_a_catalogue_too_large_for_the_walk_never_draws_a_confident_zero(
    messy_store: MerchantStore,
) -> None:
    """The path a partial count takes to the screen, and where it is stopped.

    A walk bounded at twenty pages over a larger catalogue counts a prefix. ``total``
    still spans the catalogue because the API reports it, so the four rows derived from
    the rows read describe something smaller than their own labels claim -- and the
    failure is silent in the worst direction. A catalogue whose out-of-stock products all
    sort past the bound renders "Listed, no stock: 0" and tells a merchant their shelves
    are full.

    One SKU per page makes the bound bite on the 247-product fixture catalogue. The four
    derived rows come back as "not measured"; ``total`` stays measured, because nothing
    derived it from a prefix.
    """
    async with Closing(CatalogueApi(messy_store, page_size=1).backend()) as http:
        health = await http.catalogue_health()
        assert health.listed + health.delisted < health.total, "the walk must really be partial"
        rows = {row["label"]: row for row in await _card_rows(http, METRIC_CATALOGUE_HEALTH)}
    assert rows["Products in catalogue"]["count"] == health.total
    assert rows["Products in catalogue"]["measured"] is True
    for label in ("Listed for sale", "Delisted", "Available now", "Listed, no stock"):
        assert rows[label]["measured"] is False, label
        assert rows[label]["count"] is None, label
        assert rows[label]["display"] == NOT_MEASURED, label


@pytest.mark.asyncio
async def test_the_read_tool_declares_the_reach_of_its_own_breakdown(
    messy_store: MerchantStore,
) -> None:
    """The card refuses the partial figures; the read hands them over and says what they are.

    The model is entitled to everything the backend derived -- it may need to say "of the
    2,000 products I could read" -- so the read does not blank the counts. What it must
    not do is let the model believe they describe the catalogue, and
    ``breakdown_is_whole`` is the one field that settles it in either direction.
    """
    whole = (
        await _toolset(AgentRole.GROWTH, InMemoryBackend(messy_store))
        .get("catalogue_health_read")
        .func(tool_context=_Ctx())
    )
    assert whole["breakdown_is_whole"] is True
    assert whole["counted"] == whole["total"]

    async with Closing(CatalogueApi(messy_store, page_size=1).backend()) as http:
        partial = (
            await _toolset(AgentRole.GROWTH, http)
            .get("catalogue_health_read")
            .func(tool_context=_Ctx())
        )
    assert partial["breakdown_is_whole"] is False
    assert partial["counted"] < partial["total"]
    assert partial["counted"] == partial["listed"] + partial["delisted"]


# ------------------------------------------------- 3. no merchant tool reaches a buyer


#: Every tool on the Growth Specialist's roster. Anything here appearing in a buyer
#: toolset is a capability crossing a boundary it was designed not to cross.
MERCHANT_TOOLS: Final[frozenset[str]] = frozenset(SPECIALIST_TOOLS[AgentRole.GROWTH])


def _never(request: httpx.Request) -> httpx.Response:
    """Building a toolset makes no call; a transport that answered would hide it if it did."""
    raise AssertionError(f"a toolset build must not talk to the API: {request.url}")


@pytest.mark.parametrize("role", [AgentRole.SHOPPING, AgentRole.CHECKOUT])
def test_a_buyer_binding_is_offered_no_merchant_tool(store: MerchantStore, role: AgentRole) -> None:
    """Asked of a backend that *can* answer the merchant reads, which is the real case.

    ``InMemoryBackend`` implements the merchant surface, so the factory has every merchant
    builder in hand while it assembles a shopping toolset. What keeps them out is the
    roster and the capability check rather than the absence of the data, and that is the
    guarantee worth pinning: a buyer session over a merchant-capable backend is the
    configuration the product actually ships.
    """
    toolset = _toolset(role, InMemoryBackend(store))
    assert not MERCHANT_TOOLS & set(toolset.names)
    assert not MERCHANT_TOOLS & set(toolset.unbuilt)
    assert not toolset.principal.can("merchant.catalogue_health.read")
    assert not toolset.principal.can("merchant.inventory_anomalies.read")
    assert not toolset.principal.can("merchant.checkout_metrics.read")


def test_a_buyer_toolset_is_the_same_whichever_backend_built_it(store: MerchantStore) -> None:
    """One backend has the merchant surface and one does not; the buyer sees no difference.

    If it made a difference, the merchant reads would be reaching a buyer session through
    a builder table rather than through a capability, and the roster would have stopped
    being the thing that decides what a principal holds.
    """
    over_memory = _toolset(AgentRole.SHOPPING, InMemoryBackend(store))
    over_http = _toolset(
        AgentRole.SHOPPING,
        HttpBackend("https://api.test", bearer="t", transport=httpx.MockTransport(_never)),
    )
    assert over_memory.names == over_http.names
    assert over_memory.unbuilt == over_http.unbuilt


# --------------------------------------------------------------------- the live half


@pytest.fixture(scope="module")
def live_token() -> Iterator[str]:
    """A freshly minted demo session, or a skip when nothing is listening."""
    try:
        response = httpx.post(
            f"{LIVE_BASE}/v1/demo/sessions",
            json={"tenant_slug": "demo"},
            timeout=LIVE_TIMEOUT_S,
        )
    except httpx.HTTPError:
        pytest.skip(f"no API at {LIVE_BASE}; start it or set ACR_API_BASE to run the live tests")
    response.raise_for_status()
    yield str(response.json()["token"])


@pytest.mark.asyncio
async def test_the_live_api_satisfies_the_same_merchant_contract(live_token: str) -> None:
    """The invariants that held for the simulator, against the running service.

    Equality with the in-memory backend is deliberately not asserted: the live tenant's
    state moves under scenario injections, so demanding identical records would make this
    a report on whoever ran a demo last. What must hold either way is the contract, and it
    is the same function both backends were checked against above.
    """
    async with Closing(HttpBackend(LIVE_BASE, bearer=live_token, timeout=LIVE_TIMEOUT_S)) as http:
        await run_the_contract(http)
        health = await http.catalogue_health()
    # The seeded demo catalogue is well inside the walk's bound, so this read is whole and
    # a console may quote the breakdown. A tenant that outgrew the bound fails this line,
    # which is the notice we want rather than a quietly partial dashboard.
    assert health.total > 0
    assert health.listed + health.delisted == health.total


@pytest.mark.asyncio
async def test_the_live_buyer_surface_parses_what_the_api_actually_sends(live_token: str) -> None:
    """The regression this file was written after: parsers agreeing with a fixture, not a service.

    ``search``, ``product`` and the basket reads all took provenance flat off the object --
    ``source`` beside ``sku`` -- which is the shape a quote uses and no other route ever
    has, and they read a ``name`` the catalogue sends as ``display_name``. Every one of
    them raised ``contract_violation`` against the real API while the unit suite passed,
    because the fixtures had been written to match the parsers. Reading a live product is
    the only assertion that could have caught it.
    """
    async with Closing(HttpBackend(LIVE_BASE, bearer=live_token, timeout=LIVE_TIMEOUT_S)) as http:
        page = await http.search("milk", Locale.EN, 3)
        assert page.hits, "the demo catalogue stocks milk"
        hit = page.hits[0]
        assert hit.name and hit.provenance.source
        assert page.provenance.catalogue_revision >= 0

        card = await http.product(hit.sku)
        assert card.sku == hit.sku
        assert card.name == hit.name
        assert card.unit_price.minor > 0 and card.unit_price.currency == "INR"
        assert card.provenance.source == page.provenance.source

        basket = await http.basket_create()
        priced = await http.basket_set_line(basket.basket_id, MILK_SKU, 2)
        assert priced.quote is not None
        assert priced.quote.total.minor > 0
        assert priced.provenance.source == page.provenance.source
