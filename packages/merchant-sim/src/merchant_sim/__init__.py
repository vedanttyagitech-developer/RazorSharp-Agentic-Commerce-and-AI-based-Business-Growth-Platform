"""Deterministic merchant simulator for the Demo Grocery Store.

Stands in for a real merchant connector: it is the authoritative source for catalogue,
inventory, price, fees and fulfilment throughout the demo. Everything here is pure and
in-memory -- no database, no network, no clock that any decision depends on, and no model
call of any kind. The same inputs produce the same outputs on every machine and every run,
which is what lets a search result or a quote stand as evidence in a proof chain.

Division of authority:

* This package is the ONLY thing that computes a cart total. See :mod:`merchant_sim.fees`.
* The Transaction Trust Kernel, not this package, decides whether a money action is
  admissible. A quote is an offer; it authorizes nothing.
* Merchant state changes only through a labelled ``SCENARIO_INJECTION``. See
  :mod:`merchant_sim.scenarios`.
"""

from .catalogue import CATALOGUE, CURRENCY, PRODUCTS_BY_SKU, Category, Product
from .errors import (
    InvalidBasketError,
    MerchantSimError,
    ScenarioError,
    UnknownSkuError,
)
from .fees import (
    BasketLine,
    CartLine,
    Quote,
    QuoteLine,
    QuoteResult,
    Unavailability,
    quote_basket,
    quote_cart,
    tax_on,
)
from .grounding import SOURCE_ID, Clock, Freshness, system_clock
from .injection import (
    SCENARIO_LABEL,
    InjectionKind,
    ScenarioInjection,
    StateDelta,
)
from .policy import BP_SCALE, DEFAULT_FEE_POLICY, FeePolicy
from .scenarios import ScenarioController
from .search import Locale, SearchHit, SearchResults, index_terms_for, search
from .store import InventoryStatus, MerchantSnapshot, MerchantStore, ProductView
from .textfold import fold_hinglish, normalize, tokenize

__all__ = [
    "BP_SCALE",
    "CATALOGUE",
    "CURRENCY",
    "DEFAULT_FEE_POLICY",
    "PRODUCTS_BY_SKU",
    "SCENARIO_LABEL",
    "SOURCE_ID",
    "BasketLine",
    "CartLine",
    "Category",
    "Clock",
    "FeePolicy",
    "Freshness",
    "InjectionKind",
    "InvalidBasketError",
    "InventoryStatus",
    "Locale",
    "MerchantSimError",
    "MerchantSnapshot",
    "MerchantStore",
    "Product",
    "ProductView",
    "Quote",
    "QuoteLine",
    "QuoteResult",
    "ScenarioController",
    "ScenarioError",
    "ScenarioInjection",
    "SearchHit",
    "SearchResults",
    "StateDelta",
    "Unavailability",
    "UnknownSkuError",
    "fold_hinglish",
    "index_terms_for",
    "normalize",
    "quote_basket",
    "quote_cart",
    "search",
    "system_clock",
    "tax_on",
    "tokenize",
]
