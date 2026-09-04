"""Money is exact. These tests exist because a rounded paisa fails admission silently."""

import pytest
from commerce_domain import CurrencyMismatchError, Money, MoneyError
from hypothesis import given
from hypothesis import strategies as st


class TestConstruction:
    def test_float_is_refused(self):
        with pytest.raises(MoneyError, match="minor must be int"):
            Money(395.00, "INR")  # type: ignore[arg-type]

    def test_bool_is_refused(self):
        # bool subclasses int; a True amount must not silently become 1 paisa.
        with pytest.raises(MoneyError, match="minor must be int"):
            Money(True, "INR")  # type: ignore[arg-type]

    def test_unknown_currency_is_refused(self):
        with pytest.raises(MoneyError, match="unsupported currency"):
            Money(100, "XYZ")

    def test_lowercase_currency_is_refused(self):
        with pytest.raises(MoneyError, match="uppercase"):
            Money(100, "inr")

    def test_negative_is_allowed(self):
        # Refunds and deltas are legitimately negative; the kernel bounds them, not the type.
        assert Money(-7200, "INR").is_negative


class TestParse:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("395.00", 39500),
            ("395", 39500),
            ("0.01", 1),
            ("1299.50", 129950),
            ("-72.00", -7200),
        ],
    )
    def test_exact_parse(self, text, expected):
        assert Money.parse(text, "INR").minor == expected

    def test_excess_precision_is_refused_not_rounded(self):
        with pytest.raises(MoneyError, match="more precision"):
            Money.parse("395.005", "INR")

    def test_zero_exponent_currency(self):
        assert Money.parse("500", "JPY").minor == 500
        with pytest.raises(MoneyError, match="more precision"):
            Money.parse("500.5", "JPY")

    def test_parse_format_roundtrip(self):
        assert Money.parse("1299.50", "INR").format_decimal() == "1299.50"

    def test_rejects_non_numeric(self):
        with pytest.raises(MoneyError, match="not a decimal amount"):
            Money.parse("three hundred", "INR")


class TestArithmetic:
    def test_currency_mismatch_is_refused(self):
        with pytest.raises(CurrencyMismatchError):
            Money(100, "INR") + Money(100, "USD")

    def test_multiply_by_float_is_refused(self):
        # A percentage discount is computed by the fee engine in integers,
        # never by scaling Money with a float.
        with pytest.raises(MoneyError, match="int quantity"):
            Money(100, "INR") * 1.18  # type: ignore[operator]

    def test_add_sub(self):
        assert (Money(34000, "INR") + Money(2500, "INR")).minor == 36500
        assert (Money(39500, "INR") - Money(34000, "INR")).minor == 5500

    def test_quantity_multiply(self):
        assert (Money(4999, "INR") * 3).minor == 14997

    def test_ordering(self):
        assert Money(34000, "INR") < Money(39500, "INR")
        assert Money(39500, "INR") >= Money(39500, "INR")


class TestAllocate:
    def test_conserves_total_with_indivisible_remainder(self):
        # 100 paise across 3 items cannot divide evenly; nothing may be lost or invented.
        parts = Money(100, "INR").allocate([1, 1, 1])
        assert sum(p.minor for p in parts) == 100
        assert sorted(p.minor for p in parts) == [33, 33, 34]

    def test_weighted_partial_refund(self):
        parts = Money(39500, "INR").allocate([34000, 5500])
        assert sum(p.minor for p in parts) == 39500

    def test_rejects_zero_weights(self):
        with pytest.raises(MoneyError):
            Money(100, "INR").allocate([0, 0])

    @given(
        total=st.integers(min_value=0, max_value=10_000_000),
        weights=st.lists(st.integers(min_value=1, max_value=1000), min_size=1, max_size=12),
    )
    def test_property_allocation_always_conserves(self, total, weights):
        parts = Money(total, "INR").allocate(weights)
        assert sum(p.minor for p in parts) == total
        assert len(parts) == len(weights)
