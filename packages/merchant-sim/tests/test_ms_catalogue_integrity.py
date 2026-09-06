"""The catalogue must hold together on its own, because nothing copies it any more.

This file used to compare the authority against copies of it. First three ways, against a
generated TypeScript product table in the merchant console; then two, against the
storefront's offline fixture in ``mock.ts``. Both copies are gone, and deleting them was
the better fix than testing them: the console reads ``GET /v1/catalogue/products`` and the
rebuilt storefront has no fixture path at all, because a storefront that quietly falls
back to invented data shows a buyer a price the kernel never agreed to.

So there is no drift left to catch, and a parity test with nothing to compare is worse
than no test -- it passes, and reads like coverage. What remains are the properties of the
authority itself, which is where they always belonged: money is integer paise, every
category a shopper can tap resolves to real products, and identifiers follow one scheme.
"""

from __future__ import annotations

import re
from collections import Counter

from merchant_sim.catalogue import CATALOGUE, PRODUCTS_BY_SKU, Category

#: ``<PRODUCT>-<CATEGORY>-<NNN>``: a code from the product, one stable code per category,
#: and a three-digit sequence. The catalogue used to carry fifteen prefixes across four
#: conventions -- personal care split over two, staples over five, bakery skipping five
#: numbers, and an iPhone numbered 16 for the phone rather than for its position -- which
#: meant reading a proof chain required learning several naming schemes first.
SKU_PATTERN = re.compile(r"^[A-Z0-9]{2,5}-[A-Z]{3,5}-\d{3}$")

#: Money and counts. A float in any of these is a bug wherever it appears: this is a
#: payments codebase, and ``2800.0`` compares equal to ``2800`` while meaning something a
#: currency cannot represent.
INTEGER_FIELDS = ("baseline_stock", "tax_bp", "delivery_promise_days")


def test_every_sku_follows_one_scheme() -> None:
    """One shape, so an identifier in an audit row is readable without a decoder ring."""
    malformed = sorted(sku for sku in PRODUCTS_BY_SKU if not SKU_PATTERN.match(sku))
    assert not malformed, f"SKUs that do not match <PRODUCT>-<CATEGORY>-<NNN>: {malformed[:12]}"


def test_skus_are_unique_and_indexed_completely() -> None:
    """``PRODUCTS_BY_SKU`` is the lookup every other module uses; a collision loses a row."""
    counts = Counter(product.sku for product in CATALOGUE)
    duplicates = sorted(sku for sku, n in counts.items() if n > 1)
    assert not duplicates, f"duplicate SKUs: {duplicates}"
    assert len(PRODUCTS_BY_SKU) == len(CATALOGUE), (
        f"the index holds {len(PRODUCTS_BY_SKU)} of {len(CATALOGUE)} products, "
        "so a duplicate silently overwrote one"
    )


def test_numbering_is_contiguous_from_001_within_each_category() -> None:
    """A gap means a product was removed and the rest were never renumbered.

    Contiguity is not cosmetic. The sequence is what makes a missing product visible: with
    gaps allowed, a SKU that disappears from the catalogue and stays referenced somewhere
    else looks exactly like a number that was never issued.
    """
    by_category: dict[str, list[int]] = {}
    for product in CATALOGUE:
        by_category.setdefault(str(product.category), []).append(int(product.sku.rsplit("-", 1)[1]))
    broken: list[str] = []
    for category, numbers in sorted(by_category.items()):
        expected = set(range(1, len(numbers) + 1))
        missing = sorted(expected - set(numbers))
        if missing:
            broken.append(f"{category}: {len(numbers)} products, missing {missing[:8]}")
    assert not broken, "numbering is not contiguous:\n  " + "\n  ".join(broken)


def test_the_category_code_in_a_sku_agrees_with_the_product_category() -> None:
    """A dairy SKU filed under snacks is a row that two systems will disagree about."""
    codes: dict[str, set[str]] = {}
    for product in CATALOGUE:
        codes.setdefault(str(product.category), set()).add(product.sku.split("-")[1])
    ambiguous = {category: sorted(seen) for category, seen in codes.items() if len(seen) > 1}
    assert not ambiguous, f"categories written with more than one code: {ambiguous}"

    reverse: dict[str, list[str]] = {}
    for category, seen in codes.items():
        reverse.setdefault(next(iter(seen)), []).append(category)
    shared = {code: sorted(cats) for code, cats in reverse.items() if len(cats) > 1}
    assert not shared, f"one code standing for several categories: {shared}"


def test_no_price_or_count_is_a_float() -> None:
    """Money is integer minor units. ``bool`` is excluded because it is an ``int`` in Python."""
    offenders: list[str] = []
    for product in CATALOGUE:
        price = product.list_price.minor
        if not isinstance(price, int) or isinstance(price, bool):
            offenders.append(f"{product.sku}.list_price.minor is {type(price).__name__}: {price!r}")
        for field in INTEGER_FIELDS:
            value = getattr(product, field)
            if not isinstance(value, int) or isinstance(value, bool):
                offenders.append(f"{product.sku}.{field} is {type(value).__name__}: {value!r}")
    assert not offenders, "money and counts must be integers:\n  " + "\n  ".join(offenders[:20])


def test_no_price_is_zero_or_negative() -> None:
    """A free or negative product would admit, quote and charge, and nobody would notice."""
    wrong = sorted(p.sku for p in CATALOGUE if p.list_price.minor <= 0)
    assert not wrong, f"products priced at or below zero: {wrong}"


def test_every_category_a_shopper_can_tap_resolves_to_products() -> None:
    """A tile that silently returns nothing is a tile that lies.

    ``electronics`` is exempt by design: it exists so the airline and marketplace
    adaptations in the roster have somewhere to land, and the grocery demo tenant
    deliberately stocks a handful rather than pretending to be a marketplace.
    """
    populated = {product.category for product in CATALOGUE}
    empty = sorted(c.value for c in Category if c not in populated)
    assert not empty, f"categories with no products behind them: {empty}"


def test_the_catalogue_is_large_enough_to_be_believable() -> None:
    """A judge who searches for something ordinary and finds nothing stops believing the rest.

    A floor rather than an exact count. Pinning the number makes this fail the day a
    product is added, and tells whoever reads the failure that the catalogue is broken
    when in fact it grew.
    """
    assert len(CATALOGUE) >= 200, (
        f"only {len(CATALOGUE)} products; the storefront looks like a demo"
    )
