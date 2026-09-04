"""Catalogue fixture integrity and the store's read surface.

The fixture is data other tests depend on, so its invariants are pinned here rather than
assumed: unique IDs, integer INR prices, sane tax rates, and the two prices that make the
free-delivery boundary testable to the paisa.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from commerce_domain import Money
from merchant_sim.catalogue import CATALOGUE, CURRENCY, PRODUCTS_BY_SKU, Category
from merchant_sim.errors import UnknownSkuError
from merchant_sim.grounding import SOURCE_ID
from merchant_sim.policy import DEFAULT_FEE_POLICY
from merchant_sim.store import MerchantStore
from merchant_sim.textfold import normalize


def frozen_clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


class TestFixtureIntegrity:
    def test_has_a_realistic_catalogue_size(self) -> None:
        assert 35 <= len(CATALOGUE) <= 60

    def test_skus_are_unique(self) -> None:
        skus = [product.sku for product in CATALOGUE]
        assert len(set(skus)) == len(skus)
        assert set(PRODUCTS_BY_SKU) == set(skus)

    def test_every_price_is_positive_integer_minor_units_in_one_currency(self) -> None:
        for product in CATALOGUE:
            assert product.list_price.currency == CURRENCY
            assert isinstance(product.list_price.minor, int)
            assert product.list_price.minor > 0

    def test_tax_rates_are_basis_points_and_cover_the_real_gst_bands(self) -> None:
        rates = {product.tax_bp for product in CATALOGUE}
        for rate in rates:
            assert 0 <= rate <= 10_000
        # More than one band, or a per-line rounding bug could never be caught.
        assert rates >= {0, 500, 1200, 1800}

    def test_every_product_carries_both_hindi_and_latin_synonyms(self) -> None:
        for product in CATALOGUE:
            assert product.synonyms_hi, f"{product.sku} has no Devanagari synonym"
            assert product.synonyms_latin, f"{product.sku} has no romanized synonym"

    def test_every_category_is_populated(self) -> None:
        used = {product.category for product in CATALOGUE}
        assert used == set(Category)

    def test_boundary_prices_are_intact(self) -> None:
        # test_fees pins the free-delivery threshold to the paisa using exactly these two.
        # If either price is "tidied", that test must be updated in the same change.
        assert PRODUCTS_BY_SKU["GRO-STPL-001"].list_price == Money(49900, CURRENCY)
        assert PRODUCTS_BY_SKU["GRO-STPL-007"].list_price == Money(16633, CURRENCY)
        assert DEFAULT_FEE_POLICY.free_delivery_threshold == Money(49900, CURRENCY)

    def test_awkward_names_are_present_for_the_tokenizer_to_chew_on(self) -> None:
        names = {product.name_en for product in CATALOGUE}
        joined = " ".join(names)
        assert "—" in joined, "no em dash in any product name"
        assert "×" in joined, "no multiplication sign in any product name"
        assert "'" in joined, "no possessive apostrophe in any product name"
        assert "é" in joined, "no accented character in any product name"
        assert "50-50" in joined, "no digits-inside-a-brand name"

    def test_one_product_carries_invisible_characters(self) -> None:
        # A no-break space and a soft hyphen, both invisible in every log and UI. This row
        # exists so that a regression in normalization is caught by a test rather than by
        # a product silently vanishing from search results during a demo.
        product = PRODUCTS_BY_SKU["GRO-HHLD-004"]
        assert " " in product.name_en
        assert "­" in product.name_en
        assert normalize(product.name_en) == "nirma washing powder 1 kg"


class TestStoreReads:
    def test_baseline_matches_the_fixture(self) -> None:
        store = MerchantStore(clock=frozen_clock)
        for product in CATALOGUE:
            view = store.get_product(product.sku)
            assert view.unit_price == product.list_price
            assert view.stock_units == product.baseline_stock
            assert view.is_listed

    def test_two_stores_are_identical(self) -> None:
        # Determinism: no seeded randomness, no import-order dependence.
        first, second = MerchantStore(clock=frozen_clock), MerchantStore(clock=frozen_clock)
        for sku in first.all_skus():
            left, right = first.get_product(sku), second.get_product(sku)
            assert (left.unit_price, left.stock_units) == (right.unit_price, right.stock_units)
        assert first.revision == second.revision == 0

    def test_unknown_sku_raises_rather_than_returning_empty(self) -> None:
        # A hallucinated product ID must not be indistinguishable from an out-of-stock one.
        store = MerchantStore(clock=frozen_clock)
        with pytest.raises(UnknownSkuError):
            store.get_product("GRO-FAKE-999")
        with pytest.raises(UnknownSkuError):
            store.check_inventory("GRO-FAKE-999")

    def test_every_read_carries_source_and_revision(self) -> None:
        store = MerchantStore(clock=frozen_clock)
        view = store.get_product("GRO-DAIRY-001")
        status = store.check_inventory("GRO-DAIRY-001")
        for freshness in (view.freshness, status.freshness):
            assert freshness.source == SOURCE_ID
            assert freshness.catalogue_revision == store.revision
            assert freshness.observed_at.tzinfo is not None

    def test_availability_combines_listing_and_stock(self) -> None:
        store = MerchantStore(clock=frozen_clock)
        status = store.check_inventory("GRO-DAIRY-001")
        assert status.is_available
        assert status.can_fulfil(1)
        assert status.can_fulfil(status.available_units)
        assert not status.can_fulfil(status.available_units + 1)

    def test_can_fulfil_refuses_a_non_positive_quantity(self) -> None:
        store = MerchantStore(clock=frozen_clock)
        with pytest.raises(ValueError, match="positive"):
            store.check_inventory("GRO-DAIRY-001").can_fulfil(0)

    def test_freshness_detects_nothing_when_nothing_changed(self) -> None:
        store = MerchantStore(clock=frozen_clock)
        stamp = store.get_product("GRO-DAIRY-001").freshness
        assert not store.is_stale(stamp)
