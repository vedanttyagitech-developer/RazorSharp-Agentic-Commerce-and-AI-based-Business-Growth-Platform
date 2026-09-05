"""Projecting a fare offer into the catalogue record the grocery engine already prices.

THE CENTRAL CLAIM OF THIS PACKAGE
---------------------------------
:class:`merchant_sim.MerchantStore`, :func:`merchant_sim.quote_basket` and the kernel's
reservation accounting are not grocery code. They are a SKU-and-quantity machine: a record
with an identity, a price, a tax rate and a count of sellable units; a line with a quantity
against that identity; a total computed once, deterministically, in integer paise.

A bookable fare is exactly that record. Its identity is flight-plus-date-plus-family, its
price is the base airfare for one passenger, its tax rate is the one
:func:`~flight_sim.fares.effective_tax_bp` derives, and its sellable units are **seats in
that fare bucket**. Its quantity is **passengers**.

So this module does not reimplement any of it. It converts a
:class:`~flight_sim.domain.FareOffer` into a :class:`merchant_sim.Product`. Everything
downstream -- the fee engine's per-line half-up tax, the canonical checkout content, the
reservation that holds units against a SKU, the admission re-quote that detects a moved
price -- then works on flights without knowing they are flights.

That is the point worth demonstrating. A seat hold is not *like* a stock reservation; it
is the same row.

WHAT THIS PACKAGE CANNOT DO ON ITS OWN
--------------------------------------
:class:`merchant_sim.MerchantStore` seeds itself from the module-level grocery
``CATALOGUE`` and looks SKUs up in the module-level ``PRODUCTS_BY_SKU``. Loading these
products into a store therefore needs one strictly-additive change there -- a ``catalogue``
constructor argument defaulting to the grocery fixture. That change was made, verified
green, and then deliberately reverted when this work was stood down. See ``README.md``.
Without it, this module is a correct and testable projection with nowhere to put its
output.

WHY THE CATEGORY IS NOT A ``merchant_sim.Category``
---------------------------------------------------
``Product.category`` is a merchandising tag that gets indexed and counted; it is a
``StrEnum``, so it is a string. The grocery ``Category`` is closed and
``test_catalogue.py`` asserts the grocery catalogue populates every one of its members, so
adding a flight member there would break a grocery test this work was not allowed to
touch. A second vertical therefore brings its own tag vocabulary in
:class:`FlightCategory`, which is also a ``StrEnum`` and therefore also a string, and
behaves identically everywhere the value is actually used.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Final

from commerce_domain import Money
from merchant_sim import Product

from .domain import CURRENCY, Airport, FareOffer, Flight
from .fares import FAMILIES, effective_tax_bp

__all__ = [
    "HINDI_CITY",
    "FlightCategory",
    "offers_for",
    "product_for",
    "products_for",
]


class FlightCategory(StrEnum):
    """The flight vertical's merchandising tags.

    Two, because domestic and international are the split a buyer, a revenue manager and a
    tax authority all agree on, and the console's category counts are useful when they say
    "412 domestic, 38 international" rather than repeating the fare family.
    """

    DOMESTIC = "domestic_flight"
    INTERNATIONAL = "international_flight"


#: Devanagari city names for the buyer-facing Hindi rendering. Absent codes fall back to
#: the IATA code, which is what an Indian traveller reads on a boarding pass anyway.
HINDI_CITY: Final[Mapping[str, str]] = {
    "DEL": "दिल्ली",
    "BOM": "मुंबई",
    "BLR": "बेंगलुरु",
    "MAA": "चेन्नई",
    "CCU": "कोलकाता",
    "HYD": "हैदराबाद",
    "AMD": "अहमदाबाद",
    "PNQ": "पुणे",
    "COK": "कोच्चि",
    "GOX": "गोवा",
    "JAI": "जयपुर",
    "LKO": "लखनऊ",
    "IXC": "चंडीगढ़",
    "GAU": "गुवाहाटी",
    "TRV": "तिरुवनंतपुरम",
    "BBI": "भुवनेश्वर",
    "PAT": "पटना",
    "VNS": "वाराणसी",
    "NAG": "नागपुर",
    "IDR": "इंदौर",
}


def _hindi(iata: str) -> str:
    return HINDI_CITY.get(iata, iata)


def offers_for(flight: Flight, dates: Sequence[dt.date]) -> Iterator[FareOffer]:
    """Every bookable offer for one flight across a date window: date x fare family.

    Ordered date-major then family-major, so the SKU list is stable across runs and a
    fixture diff reads as a schedule diff.
    """
    for date in dates:
        for fam in FAMILIES:
            yield FareOffer(flight=flight, date=date, family=fam)


def product_for(
    offer: FareOffer,
    *,
    origin: Airport,
    destination: Airport,
    international: bool = False,
) -> Product:
    """One fare offer as a catalogue record the grocery fee engine can price.

    Field by field, so a reviewer can check the mapping rather than trust it:

    * ``sku`` is flight-date-family, the bookable identity.
    * ``name_en`` / ``name_hi`` are the one-line description the approval card prints. A
      buyer must be able to tell *which flight* they are approving from that line alone.
    * ``unit_label`` is ``per passenger``: the unit the price is quoted in, exactly as a
      grocery record says ``500 ml``.
    * ``list_price`` is this family's base airfare for one passenger, before tax.
    * ``baseline_stock`` is **seats released into this fare bucket**. This is the field the
      kernel's reservation accounting holds units against, which is what makes a seat hold
      and a stock hold the same mechanism.
    * ``tax_bp`` is the per-SKU effective rate that reproduces GST plus the flat statutory
      charges for the *departure* airport. Per SKU, because flat charges cannot be a rate
      in general -- only for one known fare.
    * ``synonyms_*`` carry the route, the cities and the flight number so the existing
      Hinglish-folding search finds "delhi mumbai", "DEL BOM" and "6E 2134" alike.
    """
    flight = offer.flight
    base = offer.base_fare_minor
    return Product(
        sku=offer.sku,
        name_en=offer.display_name(origin=origin, destination=destination),
        name_hi=(
            f"{flight.number} · {_hindi(origin.iata)}–{_hindi(destination.iata)} · "
            f"{offer.date:%d/%m} · {flight.departs} · {offer.family.label}"
        ),
        # A StrEnum from this package rather than merchant_sim's closed grocery one; see
        # the module docstring. It is a str and behaves as one everywhere it is consumed.
        category=(  # type: ignore[arg-type]
            FlightCategory.INTERNATIONAL if international else FlightCategory.DOMESTIC
        ),
        unit_label="per passenger",
        list_price=Money(base, CURRENCY),
        baseline_stock=offer.baseline_seats,
        tax_bp=effective_tax_bp(base, origin_iata=origin.iata),
        synonyms_hi=(
            _hindi(origin.iata),
            _hindi(destination.iata),
            f"{_hindi(origin.iata)} से {_hindi(destination.iata)}",
            "उड़ान",
        ),
        synonyms_latin=(
            origin.iata.lower(),
            destination.iata.lower(),
            origin.city.lower(),
            destination.city.lower(),
            f"{origin.iata.lower()} {destination.iata.lower()}",
            f"{origin.city.lower()} {destination.city.lower()}",
            flight.compact_number.lower(),
            flight.number.lower(),
            offer.family.label.lower(),
            "flight",
            "indigo",
        ),
    )


def products_for(
    flights: Iterable[Flight],
    *,
    dates: Sequence[dt.date],
    airports: Mapping[str, Airport],
    international: frozenset[str] = frozenset(),
) -> tuple[Product, ...]:
    """The whole catalogue: every flight, every date in the window, every fare family.

    ``international`` names the IATA codes that make an offer international, which is a
    property of the route rather than of the aircraft. Raises rather than skipping when a
    flight names an airport the fixture does not define, because a route to an airport with
    no name is a route no buyer could be shown.
    """
    if not dates:
        raise ValueError("a catalogue needs at least one departure date")
    out: list[Product] = []
    for flight in flights:
        try:
            origin = airports[flight.origin]
            destination = airports[flight.destination]
        except KeyError as exc:
            raise ValueError(
                f"{flight.number}: no airport record for {exc.args[0]!r}; "
                "every route endpoint must be nameable"
            ) from None
        is_intl = flight.destination in international or flight.origin in international
        for offer in offers_for(flight, dates):
            if offer.baseline_seats <= 0:
                # A bucket with no seats is not a thing to sell. Skipping it keeps the
                # catalogue honest rather than listing a fare nobody can buy.
                continue
            out.append(
                product_for(offer, origin=origin, destination=destination, international=is_intl)
            )
    return tuple(out)
