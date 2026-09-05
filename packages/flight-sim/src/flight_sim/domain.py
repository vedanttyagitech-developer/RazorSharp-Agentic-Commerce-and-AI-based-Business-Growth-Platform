"""What an airline sells, as immutable records.

Three nouns, and the distinction between them is the whole mapping onto this platform:

* :class:`Airport` and :class:`Flight` are *schedule*: a route, a departure time, a block
  time, an aircraft. They repeat daily and carry no live price and no live seat count,
  exactly as :class:`merchant_sim.Product` carries no stock. Schedule is fixture data.
* :class:`FareFamily` is *product design*: what the ticket includes. IndiGo sells four
  economy tiers and the difference between them is baggage, change rights and a seat.
* :class:`FareOffer` is the bookable thing -- one flight, on one date, in one fare family.
  It is what gets a SKU, a price and a seat count, and it is what a buyer approves.

Nothing here holds live state. Live price and live seats belong in the store, changeable
only through a labelled scenario injection, for the same reason groceries keep them apart:
a fare injection must not edit the fixture in place.

TIME
----
``dep_minute`` and ``arr_minute`` are minutes past midnight in the airport's own local
time, as integers. Never a float, never a naive datetime that a timezone could move. A
sector that lands the next calendar day carries ``arrives_next_day`` rather than an
arrival past 1440, so "23:30 to 01:45" reads the way a boarding pass reads.

MONEY
-----
Every amount is integer paise. ``base_fare_minor`` is the base airfare for one adult
before taxes and statutory fees, which is the number an airline actually files; the taxes
are derived in :mod:`flight_sim.fares` and computed by the grocery fee engine, which is
the only thing in this platform allowed to total anything up.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "CARRIER_CODE",
    "CURRENCY",
    "Aircraft",
    "Airport",
    "FareFamily",
    "FareOffer",
    "Flight",
    "hhmm",
    "minute_of_day",
]

#: The simulated carrier. One airline, because a multi-carrier fixture would need
#: interline settlement rules that this demonstration makes no claim about.
CARRIER_CODE: Final[str] = "6E"

#: Domestic Indian ticketing. International sectors in the fixture are still sold in INR,
#: which is what an Indian point-of-sale booking does.
CURRENCY: Final[str] = "INR"


class Aircraft(StrEnum):
    """Equipment. Determines nothing financial; it is shown to the buyer and it is why a
    620 km sector can legitimately take 95 minutes when an ATR flies it."""

    A320 = "A320"
    A320NEO = "A320neo"
    A321NEO = "A321neo"
    A321XLR = "A321XLR"
    ATR72 = "ATR 72-600"
    B787 = "B787-9"
    A350 = "A350-900"


@dataclass(frozen=True, slots=True)
class Airport:
    """One airport. ``iata`` is identity; everything else is presentation."""

    iata: str
    city: str
    name: str
    terminal: str | None = None

    def __post_init__(self) -> None:
        if len(self.iata) != 3 or not self.iata.isalpha() or not self.iata.isupper():
            raise ValueError(f"{self.iata!r} is not an IATA code")
        if not self.city or not self.name:
            raise ValueError(f"{self.iata}: city and name are buyer-visible and required")

    @property
    def label(self) -> str:
        """``BOM, T2`` -- how a boarding pass names the point."""
        return f"{self.iata}, {self.terminal}" if self.terminal else self.iata


def minute_of_day(text: str) -> int:
    """``"06:05"`` -> 365. Refuses anything that is not a 24-hour clock time."""
    hours, _, minutes = text.partition(":")
    if len(hours) != 2 or len(minutes) != 2 or not hours.isdigit() or not minutes.isdigit():
        raise ValueError(f"{text!r} is not HH:MM")
    h, m = int(hours), int(minutes)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"{text!r} is not a time of day")
    return h * 60 + m


def hhmm(minute: int) -> str:
    """365 -> ``"06:05"``. The inverse, used for every buyer-visible time."""
    if not 0 <= minute < 1440:
        raise ValueError(f"{minute} is not a minute of the day")
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True, slots=True)
class Flight:
    """One scheduled daily departure. Immutable schedule data; no live price, no live seats."""

    number: str
    origin: str
    destination: str
    dep_minute: int
    arr_minute: int
    duration_minutes: int
    aircraft: Aircraft
    distance_km: int
    arrives_next_day: bool = False
    #: Base airfare for one adult, in paise, before taxes and statutory fees. The
    #: *baseline*: the live fare is read from the store, never from here.
    base_fare_minor: int = 0
    #: Economy seats the airline is willing to sell on this departure, across all fare
    #: families. Baseline; the live count is the store's.
    baseline_seats: int = 0

    def __post_init__(self) -> None:
        if not self.number.startswith(f"{CARRIER_CODE} "):
            raise ValueError(f"{self.number!r} is not a {CARRIER_CODE} flight number")
        digits = self.number.removeprefix(f"{CARRIER_CODE} ")
        if not digits.isdigit() or not 1 <= len(digits) <= 4:
            raise ValueError(f"{self.number!r} has no plausible flight number")
        if self.origin == self.destination:
            raise ValueError(f"{self.number}: a flight must go somewhere")
        for label, minute in (("dep", self.dep_minute), ("arr", self.arr_minute)):
            if not 0 <= minute < 1440:
                raise ValueError(
                    f"{self.number}: {label}_minute {minute} is not a minute of the day"
                )
        if self.duration_minutes <= 0:
            raise ValueError(f"{self.number}: block time must be positive")
        if self.distance_km <= 0:
            raise ValueError(f"{self.number}: distance must be positive")
        if self.base_fare_minor <= 0:
            raise ValueError(f"{self.number}: a base fare must be positive paise")
        if self.baseline_seats < 0:
            raise ValueError(f"{self.number}: seats cannot be negative")

    @property
    def route(self) -> str:
        """``DEL-BOM``. The key every route search is indexed by."""
        return f"{self.origin}-{self.destination}"

    @property
    def compact_number(self) -> str:
        """``6E2134`` -- the SKU-safe form, with no space to escape."""
        return self.number.replace(" ", "")

    @property
    def departs(self) -> str:
        return hhmm(self.dep_minute)

    @property
    def arrives(self) -> str:
        return hhmm(self.arr_minute)

    @property
    def duration_label(self) -> str:
        """``02h 15m`` -- IndiGo's own rendering of block time."""
        return f"{self.duration_minutes // 60:02d}h {self.duration_minutes % 60:02d}m"


@dataclass(frozen=True, slots=True)
class FareFamily:
    """One purchasable tier, and precisely what it includes.

    The inclusions are buyer-visible copy AND the receipt's policy terms: a Policy-at-Sale
    Receipt freezes the change and cancellation rights that were in force at the moment of
    sale, and for an airline those rights *are* the fare family. A buyer who paid for
    Flexi Plus and is later told the ticket is non-changeable can point at the receipt.
    """

    code: str
    label: str
    #: Multiplier over the flight's base fare, in basis points. 10000 is the base fare
    #: itself. Integer, because a float multiplier is a float that touched an amount.
    multiplier_bp: int
    cabin_bag_kg: int
    checkin_bag_kg: int
    #: Change fee treatment, as the receipt records it and the storefront prints it.
    change_policy: str
    cancellation_policy: str
    perks: tuple[str, ...] = ()
    #: Share of the departure's seats the airline releases into this bucket, in basis
    #: points of the total. The buckets sum to 10000; the store holds the seats.
    seat_share_bp: int = 0

    def __post_init__(self) -> None:
        if not self.code.isupper() or not self.code.isascii() or " " in self.code:
            raise ValueError(f"{self.code!r} must be an uppercase SKU-safe code")
        if self.multiplier_bp < 10_000:
            raise ValueError(f"{self.code}: a fare family never prices below the base fare")
        if self.cabin_bag_kg <= 0 or self.checkin_bag_kg < 0:
            raise ValueError(f"{self.code}: implausible baggage allowance")
        if not 0 <= self.seat_share_bp <= 10_000:
            raise ValueError(f"{self.code}: seat share must be basis points of the departure")


@dataclass(frozen=True, slots=True)
class FareOffer:
    """One flight, on one date, in one fare family: the bookable unit that gets a SKU.

    This is the join that makes the mapping work. A grocery SKU is a thing you can put a
    quantity against and buy; so is this, where the quantity is passengers.
    """

    flight: Flight
    date: dt.date
    family: FareFamily

    @property
    def sku(self) -> str:
        """``6E2134-20260912-SAVER``.

        Three components because all three are needed to identify what was sold: the same
        flight number on the next day is a different departure with its own seats, and the
        same departure in another fare family is a different product at a different price.
        Hyphen-separated and uppercase so it reads in a log and survives a URL.
        """
        return f"{self.flight.compact_number}-{self.date:%Y%m%d}-{self.family.code}"

    @property
    def base_fare_minor(self) -> int:
        """This family's base airfare in paise, from the flight's base and the multiplier.

        Integer arithmetic with half-up rounding, the same rule the fee engine uses, so a
        fare family never introduces a rounding convention of its own.
        """
        return (self.flight.base_fare_minor * self.family.multiplier_bp + 5_000) // 10_000

    @property
    def baseline_seats(self) -> int:
        """Seats released into this family's bucket on this departure."""
        return (self.flight.baseline_seats * self.family.seat_share_bp) // 10_000

    def display_name(self, *, origin: Airport, destination: Airport) -> str:
        """What the buyer sees on the approval card and in the checkout content.

        Everything a person needs to recognise the thing they are about to pay for, in one
        line, because the approval card has one line per item: carrier and number, route,
        date, departure time, and the fare family. A buyer who cannot tell from the
        approval card which flight they are buying cannot meaningfully approve it.
        """
        return (
            f"{self.flight.number} · {origin.iata}–{destination.iata} · "
            f"{self.date:%a %d %b} · {self.flight.departs} · {self.family.label}"
        )
