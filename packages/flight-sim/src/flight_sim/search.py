"""Route-and-date search: what a flight buyer actually asks for.

A grocery buyer asks by token -- "milk", "doodh" -- and :mod:`merchant_sim.search` folds
Hinglish and ranks by term overlap. A flight buyer asks by *origin, destination and date*,
and the answer is not a ranked list of loosely matching things: it is the exact set of
departures on that city pair on that day, with a price per fare family beside each. Token
search over thousands of fare SKUs would be the wrong instrument, and would rank
"6E 2134 Saver" above "6E 2134 Lite" for reasons no traveller would accept.

So this module indexes the schedule by route and returns whole departures with their fare
trays attached, exactly as the results page presents them.

This is the one place where the flight vertical genuinely needs its own code rather than
the grocery engine's. It is worth being precise about why: the *pricing* and the
*inventory* generalise perfectly, and the *discovery* does not, because discovery is where
a vertical's shape actually lives.

GROUNDING
---------
Every result carries the store's :class:`~merchant_sim.grounding.Freshness`, and every
price in it is read from the live store rather than from the schedule fixture. An agent may
therefore only learn a fare from a search result, and a search result taken before a fare
injection can be *proved* stale by its revision -- which is the whole version N to N+1
story, told in fares instead of grocery prices.

There is no ranking model and no scoring heuristic here. The order is departure time,
because that is the order a traveller reads a day in.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from commerce_domain import Money
from merchant_sim import Freshness, MerchantStore, UnknownSkuError

from .domain import Airport, FareOffer, Flight
from .fares import FAMILIES
from .projection import FlightCategory

__all__ = [
    "MAX_RESULTS",
    "FareOption",
    "FlightResult",
    "RouteSearchResults",
    "category_of",
    "destinations_from",
    "search_flights",
]

#: Departures returned for one route and date. A city pair with more than this many is a
#: fixture problem, not a page the buyer wants; the cap is reported in the result rather
#: than applied silently, because a silent truncation reads as "that is all there is".
MAX_RESULTS: Final[int] = 60


@dataclass(frozen=True, slots=True)
class FareOption:
    """One fare family on one departure, priced from live store state.

    ``available_seats`` is the bucket's live count. It is reported rather than hidden when
    zero, because "this fare is sold out on this flight" is a different and more useful
    answer than the fare silently not existing.
    """

    sku: str
    family_code: str
    family_label: str
    base_fare: Money
    tax: Money
    total: Money
    available_seats: int
    is_available: bool
    cabin_bag_kg: int
    checkin_bag_kg: int
    change_policy: str
    cancellation_policy: str
    perks: tuple[str, ...]

    @property
    def sold_out(self) -> bool:
        return not self.is_available


@dataclass(frozen=True, slots=True)
class FlightResult:
    """One departure and its whole fare tray, as the results page shows it."""

    flight: Flight
    date: dt.date
    origin: Airport
    destination: Airport
    fares: tuple[FareOption, ...]

    @property
    def cheapest(self) -> FareOption | None:
        """The lowest total among the fares that can actually be bought.

        ``None`` when every bucket is sold out, which a storefront renders as a sold-out
        departure rather than as a price of zero. A "starts at" price must never name a
        fare the buyer cannot purchase.
        """
        sellable = [f for f in self.fares if f.is_available]
        if not sellable:
            return None
        return min(sellable, key=lambda f: f.total.minor)

    @property
    def any_available(self) -> bool:
        return self.cheapest is not None


@dataclass(frozen=True, slots=True)
class RouteSearchResults:
    """Everything on one city pair on one date, plus the provenance of the read."""

    origin: Airport
    destination: Airport
    date: dt.date
    results: tuple[FlightResult, ...]
    freshness: Freshness
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.results

    @property
    def cheapest_total(self) -> Money | None:
        """The lowest bookable total across the whole day, or ``None`` if nothing sells."""
        totals = [r.cheapest.total for r in self.results if r.cheapest is not None]
        return min(totals, key=lambda m: m.minor) if totals else None


def _fare_option(offer: FareOffer, *, store: MerchantStore) -> FareOption | None:
    """Price one offer from live store state, or ``None`` if the store never listed it.

    Returns ``None`` rather than raising for an unknown SKU: a date outside the store's
    materialised window is a legitimate "nothing here", whereas raising would turn a search
    for next month into an error page.
    """
    try:
        view = store.get_product(offer.sku)
    except UnknownSkuError:
        return None
    base = view.unit_price
    # Tax comes from the same half-up rule the fee engine applies, on the same rate the
    # catalogue record carries, so a search result and a quote can never disagree.
    tax = Money((base.minor * view.product.tax_bp + 5_000) // 10_000, base.currency)
    fam = offer.family
    return FareOption(
        sku=offer.sku,
        family_code=fam.code,
        family_label=fam.label,
        base_fare=base,
        tax=tax,
        total=base + tax,
        available_seats=view.stock_units,
        is_available=view.is_available,
        cabin_bag_kg=fam.cabin_bag_kg,
        checkin_bag_kg=fam.checkin_bag_kg,
        change_policy=fam.change_policy,
        cancellation_policy=fam.cancellation_policy,
        perks=fam.perks,
    )


def search_flights(
    *,
    origin: str,
    destination: str,
    date: dt.date,
    store: MerchantStore,
    routes: Mapping[str, Sequence[Flight]],
    airports: Mapping[str, Airport],
    limit: int = MAX_RESULTS,
) -> RouteSearchResults:
    """Every departure on one city pair on one date, each with its fare tray.

    ``routes`` is the schedule indexed by ``"DEL-BOM"``; ``store`` is live merchant state.
    The split is deliberate and is the same split groceries make: the schedule is immutable
    fixture data, the prices and seat counts are volatile state that only a labelled
    injection changes.

    An unknown airport code raises, because a search for an airport this airline does not
    serve is a different answer from a search that found nothing, and collapsing the two
    would let a hallucinated IATA code look like a quiet day.
    """
    origin_code, destination_code = origin.upper(), destination.upper()
    for code in (origin_code, destination_code):
        if code not in airports:
            raise UnknownSkuError(f"no airport {code!r} in this airline's network")
    if origin_code == destination_code:
        raise ValueError("origin and destination must differ")
    if limit <= 0:
        raise ValueError("limit must be positive")

    scheduled = routes.get(f"{origin_code}-{destination_code}", ())
    ordered = sorted(scheduled, key=lambda f: (f.dep_minute, f.number))
    truncated = len(ordered) > limit

    results: list[FlightResult] = []
    for flight in ordered[:limit]:
        fares = tuple(
            option
            for fam in FAMILIES
            if (option := _fare_option(FareOffer(flight, date, fam), store=store)) is not None
        )
        if not fares:
            # The store has no SKU for this departure on this date: outside the window.
            continue
        results.append(
            FlightResult(
                flight=flight,
                date=date,
                origin=airports[origin_code],
                destination=airports[destination_code],
                fares=fares,
            )
        )

    return RouteSearchResults(
        origin=airports[origin_code],
        destination=airports[destination_code],
        date=date,
        results=tuple(results),
        freshness=store.freshness(),
        truncated=truncated,
    )


def destinations_from(
    origin: str, *, routes: Mapping[str, Sequence[Flight]], airports: Mapping[str, Airport]
) -> tuple[Airport, ...]:
    """Every airport this airline serves non-stop from ``origin``, alphabetically by city.

    Used by a destination picker, which must offer only cities the fixture can actually
    price: an autocomplete that suggests a route with no flights is how a buyer ends up on
    an empty results page believing the airline cancelled something.
    """
    code = origin.upper()
    seen = {key.split("-", 1)[1] for key in routes if key.startswith(f"{code}-") and routes[key]}
    known = [airports[c] for c in seen if c in airports]
    return tuple(sorted(known, key=lambda a: (a.city, a.iata)))


def category_of(destination_iata: str, *, international: frozenset[str]) -> FlightCategory:
    """Which merchandising tag a route to this airport carries."""
    return (
        FlightCategory.INTERNATIONAL
        if destination_iata.upper() in international
        else FlightCategory.DOMESTIC
    )
