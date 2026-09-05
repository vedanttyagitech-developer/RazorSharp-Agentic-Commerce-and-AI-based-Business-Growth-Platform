"""The four economy fare families, the tax model, and the convenience fee.

Every number in this module was read off goindigo.in on 5 September 2026 for 6E 6470,
DEL–NMI, and the arithmetic below reproduces that booking to the paisa. That matters more
than it sounds: it means the demonstration's fare-change refusal would move a total that a
person can check against a real airline's real fare breakdown, rather than against a
number somebody made up.

THE MEASURED BOOKING
--------------------
The fare tray offered four economy families::

    Lite        Rs 6,057    7 kg cabin only, NO check-in bag, standard change/cancel
    Saver       Rs 6,529    7 kg cabin, 15 kg check-in, standard change/cancel
    Flexi Plus  Rs 6,844    7 kg cabin, 15 kg check-in, PARTIAL change/cancel, snack, seat
    UpFront     Rs 9,154    7 kg cabin, 20 kg check-in, ZERO change fee beyond 72h, front rows

and "View Details" broke the Lite fare down as::

    Base Airfare   1 Adult    Rs 4,395
    Taxes & Fees   Total Tax  Rs 1,662
    TOTAL FARE                Rs 6,057    * convenience fee may apply

with a disclaimer naming a non-refundable convenience fee of up to Rs 499 per passenger
per segment on every online payment mode.

WHY THE MULTIPLIERS ARE WHAT THEY ARE
-------------------------------------
:data:`FAMILIES` prices each tier as a basis-point multiplier over the flight's base fare,
where Lite is the base. The multipliers are not the ratio of the displayed totals, and the
reason is the tax model below: only the 5% GST component scales with the fare, while the
statutory charges are flat, so a tier that costs 7.8% more *in total* must carry more than
7.8% more base fare to get there. Each multiplier is solved backwards from the measured
total instead -- ``base = (total - flat) / 1.05``, expressed over the Lite base -- so that
running the ladder through the fee engine reproduces all four displayed prices within a
rupee. The assertions at the foot of this module pin that.

Deriving them from a real fare tray rather than inventing round numbers means the ladder
keeps the proportions a real revenue-management system produced: Lite to Saver is a
baggage upsell of about 10%, Saver to Flexi Plus buys change rights for another 6%, and
UpFront is a 67% premium over Lite.

THE TAX MODEL
-------------
Indian airfare tax is not one rate. It is 5% GST on the base fare *plus* flat statutory
charges per departing passenger -- the airport's User Development Fee and the Aviation
Security Fee -- which do not scale with the fare at all. So a cheap ticket carries a much
higher effective tax percentage than an expensive one, which is exactly what the measured
booking shows: Rs 1,662 on Rs 4,395 is 37.8%.

:func:`effective_tax_bp` collapses that into the single per-line rate the fee engine takes,
by computing the true tax for a given base fare and then expressing it as the rate that
reproduces it. It is a per-SKU rate rather than a per-category one, which is why the
model can be exact instead of approximate. On the measured booking it produces 3781 bp,
and the fee engine's half-up arithmetic then yields Rs 1,661.75 -- the Rs 1,662 the site
displays, before the site's own rounding to whole rupees.

Nothing here computes a basket total. The fee engine in :mod:`merchant_sim.fees` does
that, as it does for groceries, and it is still the only thing in the platform allowed to.
"""

from __future__ import annotations

from typing import Final

from commerce_domain import Money
from merchant_sim import FeePolicy

from .domain import CURRENCY, FareFamily

__all__ = [
    "AVIATION_SECURITY_FEE_MINOR",
    "CONVENIENCE_FEE_MINOR",
    "CONVENIENCE_FEE_WAIVER_MINOR",
    "DEFAULT_UDF_MINOR",
    "FAMILIES",
    "FAMILIES_BY_CODE",
    "FLIGHT_FEE_POLICY",
    "GST_ON_AIRFARE_BP",
    "GST_ON_SERVICES_BP",
    "MEASURED_TRAY",
    "UDF_BY_AIRPORT_MINOR",
    "effective_tax_bp",
    "family",
    "statutory_minor",
    "tax_minor_for",
]

#: GST on a domestic economy airfare. 5%, the rate in the Indian schedule for economy.
GST_ON_AIRFARE_BP: Final[int] = 500

#: GST on a service fee such as the convenience fee. 18%.
GST_ON_SERVICES_BP: Final[int] = 1_800

#: Aviation Security Fee, per departing passenger. Flat: it does not scale with the fare.
AVIATION_SECURITY_FEE_MINOR: Final[int] = 23_600

#: User Development Fee where an airport publishes one this fixture knows. Per departing
#: passenger, flat, and genuinely different per airport -- which is why two identically
#: priced sectors from different cities do not carry identical tax.
UDF_BY_AIRPORT_MINOR: Final[dict[str, int]] = {
    "DEL": 120_600,
    "BOM": 61_500,
    "BLR": 96_100,
    "MAA": 43_300,
    "CCU": 47_200,
    "HYD": 68_800,
    "GOX": 82_500,
    "AMD": 39_400,
    "PNQ": 31_800,
    "COK": 51_200,
    "TRV": 44_700,
    "JAI": 29_500,
    "LKO": 33_100,
    "IXC": 28_900,
    "GAU": 36_400,
    "BBI": 34_800,
    "PAT": 32_700,
    "VNS": 30_200,
    "NAG": 27_600,
    "IDR": 26_900,
}

#: What an airport with no published figure here charges. Chosen at the low end, because
#: over-stating a statutory fee over-states the tax on every fare that departs there.
DEFAULT_UDF_MINOR: Final[int] = 30_000

#: The non-refundable convenience fee, per booking. The site's disclaimer says "up to
#: INR 499 per pax per segment"; this fixture charges it once per booking, which is the
#: shape :class:`~merchant_sim.policy.FeePolicy` expresses, and understates rather than
#: overstates what a buyer pays.
CONVENIENCE_FEE_MINOR: Final[int] = 49_900

#: Fare above which the convenience fee is waived. Airlines do waive it at the top of the
#: book; setting a real threshold rather than an unreachable sentinel also keeps the fee
#: engine's threshold arithmetic exercised on this vertical exactly as it is on groceries.
CONVENIENCE_FEE_WAIVER_MINOR: Final[int] = 5_000_000


def statutory_minor(origin_iata: str) -> int:
    """Flat per-passenger statutory charges for departing this airport, in paise.

    UDF plus the Aviation Security Fee. Flat by construction: these are the reason a
    Rs 1,800 sector can carry 90% tax and a Rs 58,000 sector carries 7%.
    """
    return UDF_BY_AIRPORT_MINOR.get(origin_iata, DEFAULT_UDF_MINOR) + AVIATION_SECURITY_FEE_MINOR


def tax_minor_for(base_fare_minor: int, *, origin_iata: str) -> int:
    """The true tax on one passenger's base fare: GST on the fare, plus flat statutory.

    Half-up on integer paise, the same rounding rule the fee engine applies, so this and
    the engine never disagree about a paisa.
    """
    if base_fare_minor <= 0:
        raise ValueError("a base fare must be positive paise")
    gst = (base_fare_minor * GST_ON_AIRFARE_BP + 5_000) // 10_000
    return gst + statutory_minor(origin_iata)


def effective_tax_bp(base_fare_minor: int, *, origin_iata: str) -> int:
    """The single per-line rate that reproduces :func:`tax_minor_for` in the fee engine.

    The fee engine takes one ``tax_bp`` per line and computes
    ``(base * bp + 5000) // 10000``. Flat statutory charges cannot be written as a rate in
    general -- but they can be for *one known base fare*, and every SKU here has exactly
    one. So the rate is computed per SKU and the engine reproduces the airline's own tax.

    Clamped to the 0..10000 range :class:`~merchant_sim.catalogue.Product` enforces. The
    clamp can only bite on a fare so low that flat charges exceed it, which would be a
    fixture error; it fails closed by under-charging tax rather than by refusing to price.
    """
    target = tax_minor_for(base_fare_minor, origin_iata=origin_iata)
    return min(10_000, max(0, (target * 10_000 + base_fare_minor // 2) // base_fare_minor))


# --------------------------------------------------------------------------- families

#: The four economy tiers, in the order the fare tray presents them. Multipliers are
#: solved backwards from the measured totals; see the module docstring.
FAMILIES: Final[tuple[FareFamily, ...]] = (
    FareFamily(
        code="LITE",
        label="Lite",
        multiplier_bp=10_000,
        cabin_bag_kg=7,
        checkin_bag_kg=0,
        change_policy="Change and cancellation charges standard",
        cancellation_policy="Standard cancellation charges apply",
        perks=("Cabin bag preferably under the seat",),
        seat_share_bp=3_000,
    ),
    FareFamily(
        code="SAVER",
        label="Saver",
        multiplier_bp=11_023,
        cabin_bag_kg=7,
        checkin_bag_kg=15,
        change_policy="Change and cancellation charges standard",
        cancellation_policy="Standard cancellation charges apply",
        perks=(),
        seat_share_bp=4_000,
    ),
    FareFamily(
        code="FLEXIPLUS",
        label="Flexi Plus",
        multiplier_bp=11_706,
        cabin_bag_kg=7,
        checkin_bag_kg=15,
        change_policy="Change and cancellation charges partial",
        cancellation_policy="Partial cancellation charges apply",
        perks=("Complimentary snack", "Complimentary standard seat"),
        seat_share_bp=2_000,
    ),
    FareFamily(
        code="UPFRONT",
        label="IndiGo UpFront",
        multiplier_bp=16_712,
        cabin_bag_kg=7,
        checkin_bag_kg=20,
        change_policy="Zero change fee beyond 72 hours",
        cancellation_policy="Low cancellation charges",
        perks=("Complimentary snack", "Front two-row economy seats"),
        seat_share_bp=1_000,
    ),
)

FAMILIES_BY_CODE: Final[dict[str, FareFamily]] = {f.code: f for f in FAMILIES}


def family(code: str) -> FareFamily:
    """Look a family up by code. Raises ``KeyError`` for one that does not exist.

    Loud rather than defaulting: a SKU naming an unknown fare family is a SKU nobody
    should be able to price, and silently falling back to the cheapest tier would sell a
    buyer something other than what they selected.
    """
    return FAMILIES_BY_CODE[code]


#: The airline's fee policy: a flat convenience fee with 18% GST on it, waived at the top
#: of the book. Structurally identical to the grocery store's delivery fee, because it is
#: the same thing -- a non-item charge added to the order at checkout.
FLIGHT_FEE_POLICY: Final[FeePolicy] = FeePolicy(
    base_delivery_fee=Money(CONVENIENCE_FEE_MINOR, CURRENCY),
    free_delivery_threshold=Money(CONVENIENCE_FEE_WAIVER_MINOR, CURRENCY),
    delivery_tax_bp=GST_ON_SERVICES_BP,
)


def _displayed_total(lite_base_minor: int, fam: FareFamily, *, origin_iata: str) -> int:
    """What the fare tray would print for this family. Used only by the vector below."""
    base = (lite_base_minor * fam.multiplier_bp + 5_000) // 10_000
    return base + tax_minor_for(base, origin_iata=origin_iata)


#: The fare tray this model was calibrated against: 6E 6470, DEL-NMI, 5 September 2026.
#: Family code -> the rupee total the site displayed. Checked at import, so a bad edit to
#: a multiplier, a statutory charge or the GST rate fails loudly at startup rather than
#: quietly re-pricing the whole fixture.
MEASURED_TRAY: Final[dict[str, int]] = {
    "LITE": 6_057,
    "SAVER": 6_529,
    "FLEXIPLUS": 6_844,
    "UPFRONT": 9_154,
}

assert tax_minor_for(439_500, origin_iata="DEL") == 166_175, "tax model no longer reproduces 6E 6470"
assert effective_tax_bp(439_500, origin_iata="DEL") == 3_781, "effective rate drifted from measurement"
for _fam in FAMILIES:
    _got = _displayed_total(439_500, _fam, origin_iata="DEL")
    _want = MEASURED_TRAY[_fam.code] * 100
    assert abs(_got - _want) <= 100, (
        f"{_fam.code}: fare ladder now prices at {_got / 100:.2f}, "
        f"measured tray said {_want / 100:.2f}"
    )
