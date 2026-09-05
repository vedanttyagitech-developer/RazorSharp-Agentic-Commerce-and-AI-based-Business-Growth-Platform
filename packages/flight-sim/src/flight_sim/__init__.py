"""A second commerce vertical: IndiGo-shaped flight booking over the same kernel.

STATUS: STOOD DOWN, 5 September 2026. This package is a complete and self-consistent
domain model with no integration. It was stopped before the storefront, the API wiring,
the agent prompts and the console tenant switcher were written. See ``README.md`` for what
was learned, what was decided, and the one seam it needs.

What is here and is finished:

* :mod:`flight_sim.domain` -- schedule, fare families, and the bookable fare offer.
* :mod:`flight_sim.fares` -- the four measured economy tiers, the GST-plus-statutory tax
  model, and the convenience fee. Reproduces a real IndiGo fare tray to within a rupee,
  checked by assertions at import.
* :mod:`flight_sim.projection` -- a fare offer as a :class:`merchant_sim.Product`, which
  is the whole mapping: seats are stock, passengers are quantity.
* :mod:`flight_sim.search` -- route-and-date search, because a flight buyer does not ask
  by token.
* :mod:`flight_sim.kernel_adapter` -- the airline's Policy-at-Sale Receipt inputs, and the
  observation that the grocery ``SimMerchantStateSource`` already satisfies admission
  step 8 for this vertical unmodified.

Nothing here touches the money path. No kernel, payment-adapter or worker code was read
into this package, and none was changed.
"""

from .domain import (
    CARRIER_CODE,
    CURRENCY,
    Aircraft,
    Airport,
    FareFamily,
    FareOffer,
    Flight,
    hhmm,
    minute_of_day,
)
from .fares import (
    FAMILIES,
    FAMILIES_BY_CODE,
    FLIGHT_FEE_POLICY,
    MEASURED_TRAY,
    effective_tax_bp,
    family,
    statutory_minor,
    tax_minor_for,
)
from .kernel_adapter import (
    FLIGHT_POLICY_VERSION,
    FLIGHT_SOURCE_ID,
    receipt_inputs_for,
    state_source_for,
)
from .projection import FlightCategory, offers_for, product_for, products_for
from .search import (
    FareOption,
    FlightResult,
    RouteSearchResults,
    destinations_from,
    search_flights,
)

__all__ = [
    "CARRIER_CODE",
    "CURRENCY",
    "FAMILIES",
    "FAMILIES_BY_CODE",
    "FLIGHT_FEE_POLICY",
    "FLIGHT_POLICY_VERSION",
    "FLIGHT_SOURCE_ID",
    "MEASURED_TRAY",
    "Aircraft",
    "Airport",
    "FareFamily",
    "FareOffer",
    "FareOption",
    "Flight",
    "FlightCategory",
    "FlightResult",
    "RouteSearchResults",
    "destinations_from",
    "effective_tax_bp",
    "family",
    "hhmm",
    "minute_of_day",
    "offers_for",
    "product_for",
    "products_for",
    "receipt_inputs_for",
    "search_flights",
    "state_source_for",
    "statutory_minor",
    "tax_minor_for",
]
