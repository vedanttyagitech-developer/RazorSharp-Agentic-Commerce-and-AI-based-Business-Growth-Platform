"""Grounded discovery over the merchant simulator. Step 1 of the demonstration.

Discovery is where grounding either happens or does not. Specification 20.1 says a model
may reference only product identifiers the merchant actually returned, and that rule is
worth nothing unless the endpoint behind it reports live merchant truth rather than a
cached list. So every value here is read from the store at one revision and stamped with
the :class:`~merchant_sim.Freshness` that names it, and the buyer surface can later prove
a price it showed was the price the merchant held.

Two decisions in this module are not obvious:

**Sold out and delisted stay apart.** ``is_listed`` and ``is_available`` are reported
separately all the way to the wire. Collapsing them is how an assistant says "we do not
sell that" about an item that is back tomorrow, and how a merchant loses the sale it
would otherwise have kept with a substitution.

**An unknown SKU is a 404, not a recovery code.** :class:`merchant_sim.UnknownSkuError`
is raised rather than returned precisely because a product identifier the merchant never
issued is a hallucination, not a shopping outcome; translating it into "out of stock"
would let a fabricated SKU travel onward as a real one that happened to be unavailable.

No database is touched. The catalogue lives in the API process (ADR 0003 D14), so these
two endpoints are pure reads of in-memory state under the registry's own consistency
rules -- a read sees the state before or after an injection, never half of one.
"""

from __future__ import annotations

import uuid

from merchant_sim import Locale, ProductView, SearchResults, UnknownSkuError, search

from ..errors import ProblemError
from ..merchants import MerchantRegistry

__all__ = ["MAX_SEARCH_LIMIT", "locale_from", "product_view", "search_catalogue"]

#: Upper bound on ``limit``. A quick-commerce catalogue answer a buyer can act on is a
#: handful of products; a caller asking for hundreds is paging through the catalogue,
#: which is a different endpoint that does not exist yet.
MAX_SEARCH_LIMIT = 50


def locale_from(raw: str | None) -> Locale:
    """Parse the ``locale`` query parameter, refusing anything unsupported.

    Refused rather than defaulted: a caller that asked for ``mr-IN`` and silently got
    English names would show the wrong script to the buyer and never learn why.
    """
    if raw is None or not raw.strip():
        return Locale.EN
    try:
        return Locale(raw.strip())
    except ValueError:
        raise ProblemError(
            422,
            "Unsupported locale",
            f"locale must be one of {', '.join(sorted(item.value for item in Locale))}.",
            field="locale",
            locale=raw,
        ) from None


def search_catalogue(
    registry: MerchantRegistry,
    *,
    merchant_id: uuid.UUID,
    query: str,
    locale: Locale,
    limit: int,
) -> SearchResults:
    """Search one merchant's live catalogue.

    Returns out-of-stock matches too, ranked below equally relevant available ones. They
    are deliberately not hidden: an assistant that cannot say "we stock that but it is
    out right now" cannot offer the substitution that keeps the sale.
    """
    if limit < 1 or limit > MAX_SEARCH_LIMIT:
        raise ProblemError(
            422,
            "Invalid limit",
            f"limit must be between 1 and {MAX_SEARCH_LIMIT}.",
            field="limit",
            limit=limit,
        )
    return search(query, locale, store=registry.store(merchant_id), limit=limit)


def product_view(registry: MerchantRegistry, *, merchant_id: uuid.UUID, sku: str) -> ProductView:
    """One product as it exists right now, or 404 for a SKU this merchant never issued."""
    try:
        return registry.store(merchant_id).get_product(sku)
    except UnknownSkuError:
        raise ProblemError(
            404,
            "Product not found",
            "No product with that SKU exists in this merchant's catalogue.",
            sku=sku,
        ) from None
