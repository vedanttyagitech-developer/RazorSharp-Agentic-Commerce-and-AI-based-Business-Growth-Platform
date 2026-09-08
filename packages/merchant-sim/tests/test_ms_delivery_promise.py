"""The merchant promises days; nobody downstream is allowed to promise a date.

For a storefront to draw "Get it by <date>" under a line, somebody has to have promised it,
and until this field existed nobody had: the one surface that printed a delivery figure
printed "8 MINS" on every card from no source, and was corrected by removing the pill. The
tempting alternative was to resolve a date somewhere in the API instead, which would have
been a number with no merchant behind it -- the exact shape of invention this platform
refuses everywhere else it touches money.

So the merchant states a duration and the surface states the date, and the tests below pin
both halves of that split:

* every catalogue row declares a promise, it is a whole number of days, and the field has
  not quietly collapsed into a constant that could have been a literal in the storefront;
* the promise arrives on the priced line exactly as the merchant declared it, so a surface
  can render it without a second read and without arithmetic;
* the promise is **absent** from the bytes a buyer's approval binds to. That is the one
  that would be expensive to get wrong: a key added to the checkout content changes the
  hash of every checkout the store can produce, and would then have to be revalidated at
  admission -- so a merchant moving a promise by a day would invalidate approvals for
  orders whose price, contents and total had not moved at all.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from commerce_domain import Money, uuid7
from merchant_adapter import content_from_quote
from merchant_sim import BasketLine, MerchantStore, quote_basket
from merchant_sim.catalogue import CATALOGUE, MAX_DELIVERY_PROMISE_DAYS, Category, Product
from merchant_sim.policy import DEFAULT_FEE_POLICY

MILK = "AMUL-DAIRY-001"  # dairy: off the dark store's shelves, same day
PHONE = "APPL-ELEC-001"  # electronics: from a warehouse, two days


def frozen_clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> MerchantStore:
    return MerchantStore(clock=frozen_clock)


def _product(*, delivery_promise_days: int) -> Product:
    """A catalogue record that is valid in every respect except the field under test."""
    return Product(
        sku="TEST-DAIRY-999",
        name_en="Test Milk 1 L",
        name_hi="टेस्ट दूध 1 लीटर",
        category=Category.DAIRY,
        unit_label="1 L",
        list_price=Money(2800, DEFAULT_FEE_POLICY.currency),
        baseline_stock=1,
        tax_bp=0,
        delivery_promise_days=delivery_promise_days,
        synonyms_hi=(),
        synonyms_latin=(),
    )


class TestTheCatalogueDeclaresIt:
    def test_every_product_states_a_promise_in_whole_days(self) -> None:
        """A float or a bool here would survive every other check in this package.

        ``bool`` is excluded explicitly because it is an ``int`` in Python, so ``True``
        would pass an ``isinstance`` test and then render as "Get it by tomorrow".
        """
        wrong = [
            f"{p.sku}: {type(p.delivery_promise_days).__name__} {p.delivery_promise_days!r}"
            for p in CATALOGUE
            if not isinstance(p.delivery_promise_days, int)
            or isinstance(p.delivery_promise_days, bool)
        ]
        assert not wrong, "delivery promises that are not whole days:\n  " + "\n  ".join(wrong)

    def test_every_promise_is_within_the_declared_bound(self) -> None:
        out_of_range = sorted(
            p.sku
            for p in CATALOGUE
            if not 0 <= p.delivery_promise_days <= MAX_DELIVERY_PROMISE_DAYS
        )
        assert not out_of_range, f"promises outside 0..{MAX_DELIVERY_PROMISE_DAYS}: {out_of_range}"

    def test_the_field_carries_a_fact_rather_than_one_number_for_the_store(self) -> None:
        """If every row agreed, the storefront could hardcode it and this would be theatre.

        Electronics are the difference and they are the reason the field is per product:
        they ship from a warehouse rather than off the dark store's shelves. A change that
        makes them same-day should have to come past this test and say so.
        """
        promises = {p.delivery_promise_days for p in CATALOGUE}
        assert len(promises) > 1, f"every product promises {promises}; the field says nothing"

        later = {str(p.category) for p in CATALOGUE if p.delivery_promise_days > 0}
        assert later == {Category.ELECTRONICS.value}, (
            f"categories that are not same-day: {sorted(later)}; the fixture's stated rule "
            "is that only electronics ship from a warehouse"
        )

    def test_a_negative_promise_cannot_be_constructed(self) -> None:
        with pytest.raises(ValueError, match="delivery_promise_days"):
            _product(delivery_promise_days=-1)

    def test_a_promise_beyond_the_bound_cannot_be_constructed(self) -> None:
        """The bound catches the typo, which is the only way a wrong promise gets in.

        Nothing else would: 200 is a perfectly good integer, and the first thing that would
        notice is a buyer reading a date seven months away.
        """
        with pytest.raises(ValueError, match="delivery_promise_days"):
            _product(delivery_promise_days=MAX_DELIVERY_PROMISE_DAYS + 1)


class TestTheQuoteCarriesItUnchanged:
    def test_a_priced_line_repeats_the_merchant_s_promise(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(MILK, 2), BasketLine(PHONE, 1)], store=store).require()
        by_sku = {line.sku: line.delivery_promise_days for line in quote.lines}
        assert by_sku == {
            MILK: store.get_product(MILK).product.delivery_promise_days,
            PHONE: store.get_product(PHONE).product.delivery_promise_days,
        }
        # And they are the two values the fixture actually declares, so a bug that copied
        # one product's promise onto every line would still fail here.
        assert by_sku == {MILK: 0, PHONE: 2}

    def test_the_promise_does_not_move_with_quantity(self, store: MerchantStore) -> None:
        """It is a property of the product, not of the cart. Nothing scales it."""
        one = quote_basket([BasketLine(PHONE, 1)], store=store).require()
        seven = quote_basket([BasketLine(PHONE, 7)], store=store).require()
        assert one.lines[0].delivery_promise_days == seven.lines[0].delivery_promise_days == 2


class TestItStaysOutOfTheBytesConsentBindsTo:
    def test_the_checkout_content_does_not_carry_a_promise(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(PHONE, 1)], store=store).require()
        content = quote.to_checkout_content()
        assert "delivery_promise_days" not in content
        for line in content["lines"]:
            assert "delivery_promise_days" not in line, line

    def test_the_kernel_s_content_document_does_not_carry_one_either(
        self, store: MerchantStore
    ) -> None:
        """``content_from_quote`` builds through the kernel's builder, which owns the shape.

        Asserted separately from the quote's own content because they are two documents
        built by two functions, and this is the one an approval is taken against.
        """
        quote = quote_basket([BasketLine(PHONE, 1)], store=store).require()
        content = content_from_quote(
            quote,
            checkout_id=uuid7(),
            version=1,
            policy_version="demo-grocery-policy/1",
        )
        assert "delivery_promise_days" not in content
        for line in content["lines"]:
            assert "delivery_promise_days" not in line, line

    def test_moving_a_promise_does_not_move_the_content_hash(self, store: MerchantStore) -> None:
        """The regression this guards is a hash that moves when a merchant moves a promise.

        If it did, every approval outstanding against that cart would be refused at
        admission as a material change to a purchase whose price, lines and total had not
        moved at all -- and the buyer would be asked to consent again to the identical
        order because the store now says Thursday instead of Wednesday.

        The two quotes below differ in the promise and in nothing else: the lines are the
        priced lines of one real quote with only that field rewritten, so the subtotals,
        the taxes and the total are the same integers on both sides.
        """
        quote = quote_basket([BasketLine(PHONE, 1), BasketLine(MILK, 2)], store=store).require()
        moved = replace(
            quote,
            lines=tuple(
                replace(line, delivery_promise_days=line.delivery_promise_days + 3)
                for line in quote.lines
            ),
        )
        assert [line.delivery_promise_days for line in moved.lines] == [5, 3]
        assert moved.content_hash() == quote.content_hash(), (
            "moving a delivery promise changed the checkout content hash; the promise has "
            "leaked into the bytes a buyer's approval binds to"
        )
