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
from sqlalchemy.orm import Session

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
    session: Session,
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
    return search(query, locale, store=registry.store(session, merchant_id), limit=limit)


#: A catalogue page for a merchant tool. Larger than a search page because a console
#: renders a table an operator scrolls, and smaller than the whole catalogue because an
#: unbounded page is a denial-of-service waiting for a bigger catalogue.
MAX_LIST_LIMIT = 100


def list_products(
    session: Session,
    registry: MerchantRegistry,
    *,
    merchant_id: uuid.UUID,
    category: str | None,
    listed: bool | None,
    available: bool | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[ProductView], str | None, int, dict[str, int]]:
    """A page of this merchant's catalogue, ordered by SKU, with counts across the filter.

    Ordered by SKU rather than by name: the SKU is the identity the rest of the platform
    uses, it is stable under a rename, and it gives the page a keyset cursor for free --
    the cursor is simply the last SKU of the previous page.

    Counts are by category across the *whole* catalogue rather than the page or the
    filter, because a merchant reading "produce: 0" wants to know the category is empty,
    not that they are looking at a filtered view of it.
    """
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise ProblemError(
            422,
            "Invalid limit",
            f"limit must be between 1 and {MAX_LIST_LIMIT}.",
            field="limit",
            limit=limit,
        )
    store = registry.store(session, merchant_id)
    everything = [store.get_product(sku) for sku in sorted(store.all_skus())]
    by_category: dict[str, int] = {}
    for view in everything:
        key = str(view.product.category)
        by_category[key] = by_category.get(key, 0) + 1

    if category is not None:
        wanted = category.strip().lower()
        if wanted not in by_category:
            raise ProblemError(
                422,
                "Unknown category",
                "Filter by one of this merchant's categories.",
                category=category,
                allowed=sorted(by_category),
            )
        everything = [v for v in everything if str(v.product.category) == wanted]
    if listed is not None:
        everything = [v for v in everything if v.is_listed is listed]
    if available is not None:
        everything = [v for v in everything if v.is_available is available]

    total = len(everything)
    if cursor is not None:
        everything = [v for v in everything if v.sku > cursor]
    page = everything[: limit + 1]
    more = len(page) > limit
    page = page[:limit]
    return page, (page[-1].sku if page and more else None), total, by_category


def product_view(
    session: Session, registry: MerchantRegistry, *, merchant_id: uuid.UUID, sku: str
) -> ProductView:
    """One product as it exists right now, or 404 for a SKU this merchant never issued."""
    try:
        return registry.store(session, merchant_id).get_product(sku)
    except UnknownSkuError:
        raise ProblemError(
            404,
            "Product not found",
            "No product with that SKU exists in this merchant's catalogue.",
            sku=sku,
        ) from None
