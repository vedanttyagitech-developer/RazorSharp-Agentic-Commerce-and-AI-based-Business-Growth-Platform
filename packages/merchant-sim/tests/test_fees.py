"""The fee engine: exact integer totals, per-line tax, and the threshold to the paisa.

Two claims are pinned hardest here, because they are the ones a buyer disputes:

1. The quoted total equals the sum of its components exactly. Not approximately, not after
   rounding -- exactly, in integer minor units.
2. The free-delivery threshold behaves correctly one paisa either side of the boundary.

The catalogue carries two prices chosen so the second claim can be tested end to end
against real products rather than against a synthetic Money value:
``GRO-STPL-001`` at 49900 paise lands exactly ON the Rs 499.00 threshold, and three units
of ``GRO-STPL-007`` at 16633 paise land exactly one paisa BELOW it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from commerce_domain import Money, canonical_hash
from merchant_sim.errors import InvalidBasketError, UnknownSkuError
from merchant_sim.fees import BasketLine, Quote, QuoteLine, QuoteResult, quote_basket, tax_on
from merchant_sim.policy import DEFAULT_FEE_POLICY, FeePolicy
from merchant_sim.scenarios import ScenarioController
from merchant_sim.store import MerchantStore
from transaction_kernel import RecoveryCode

INR = "INR"
RICE = "GRO-STPL-001"  # 49900 paise, 5% GST -- exactly on the threshold
OIL = "GRO-STPL-007"  # 16633 paise, 5% GST -- 3 units are one paisa below
MILK = "GRO-DAIRY-001"  # 2800 paise, 0% GST
ATTA = "GRO-STPL-002"  # 25500 paise, 5% GST
DAHI = "GRO-DAIRY-003"  # 4500 paise, 5% GST


def frozen_clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> MerchantStore:
    return MerchantStore(clock=frozen_clock)


def inr(minor: int) -> Money:
    return Money(minor, INR)


class TestTaxArithmetic:
    def test_half_up_rounding_is_exact_at_the_boundary(self) -> None:
        # 1 paise at 50% is exactly 0.5 paise. Half-up takes it to 1, every time, on every
        # machine. Banker's rounding would take it to 0 here and to 1 next door.
        assert tax_on(inr(1), 5000) == inr(1)
        assert tax_on(inr(3), 5000) == inr(2)  # 1.5 -> 2
        assert tax_on(inr(1), 4999) == inr(0)  # 0.4999 -> 0

    def test_known_gst_values(self) -> None:
        assert tax_on(inr(49899), 500) == inr(2495)  # 2494.95 -> 2495
        assert tax_on(inr(49900), 500) == inr(2495)  # exactly 2495
        assert tax_on(inr(2500), 1800) == inr(450)  # exactly 450
        assert tax_on(inr(10000), 0) == inr(0)

    def test_result_is_always_an_integer_number_of_minor_units(self) -> None:
        for base in range(0, 5000, 137):
            for rate in (0, 500, 1200, 1800, 2800):
                tax = tax_on(inr(base), rate)
                assert isinstance(tax.minor, int)
                assert not isinstance(tax.minor, bool)

    def test_refuses_a_negative_base_and_an_out_of_range_rate(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            tax_on(inr(-100), 500)
        with pytest.raises(ValueError, match="rate_bp"):
            tax_on(inr(100), 10_001)
        with pytest.raises(ValueError, match="rate_bp"):
            tax_on(inr(100), -1)


class TestTotalEqualsItsComponents:
    def test_total_is_the_exact_sum_for_a_mixed_tax_basket(self, store: MerchantStore) -> None:
        quote = quote_basket(
            [BasketLine(MILK, 2), BasketLine(ATTA, 1), BasketLine(DAHI, 1)], store=store
        ).require()
        # Recomputed independently of the engine, from the published inputs.
        assert quote.items_subtotal == inr(2800 * 2 + 25500 + 4500)
        assert quote.items_tax == inr(0 + 1275 + 225)
        assert quote.delivery_fee == inr(2500)
        assert quote.delivery_tax == inr(450)
        assert quote.total == inr(35600 + 1500 + 2500 + 450)
        assert quote.total == (
            quote.items_subtotal + quote.items_tax + quote.delivery_fee + quote.delivery_tax
        )

    def test_line_subtotals_and_taxes_sum_to_the_stated_totals(self, store: MerchantStore) -> None:
        quote = quote_basket(
            [BasketLine(MILK, 3), BasketLine(OIL, 2), BasketLine(DAHI, 5)], store=store
        ).require()
        assert quote.items_subtotal == sum((line.subtotal for line in quote.lines), Money.zero(INR))
        assert quote.items_tax == sum((line.tax for line in quote.lines), Money.zero(INR))

    def test_a_quote_whose_total_disagrees_cannot_be_constructed(
        self, store: MerchantStore
    ) -> None:
        # This is the guard that makes "the fee engine is the only thing that computes a
        # total" enforceable rather than aspirational.
        good = quote_basket([BasketLine(MILK, 2)], store=store).require()
        with pytest.raises(ValueError, match="does not equal the sum of its components"):
            Quote(
                lines=good.lines,
                items_subtotal=good.items_subtotal,
                items_tax=good.items_tax,
                delivery_fee=good.delivery_fee,
                delivery_tax=good.delivery_tax,
                total=good.total - inr(1),  # one paisa "adjustment"
                free_delivery_applied=good.free_delivery_applied,
                gap_to_free_delivery=good.gap_to_free_delivery,
                currency=INR,
                freshness=good.freshness,
            )

    def test_a_line_whose_subtotal_is_not_price_times_quantity_is_refused(self) -> None:
        with pytest.raises(ValueError, match="price x quantity"):
            QuoteLine(
                sku=MILK,
                name="Milk",
                unit_price=inr(2800),
                quantity=2,
                subtotal=inr(5000),
                tax_bp=0,
                tax=inr(0),
            )

    def test_a_line_whose_tax_does_not_match_its_rate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tax does not match"):
            QuoteLine(
                sku=DAHI,
                name="Dahi",
                unit_price=inr(4500),
                quantity=1,
                subtotal=inr(4500),
                tax_bp=500,
                tax=inr(300),
            )

    def test_quantity_multiplies_exactly_at_scale(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(OIL, 13)], store=store).require()
        assert quote.items_subtotal == inr(16633 * 13)
        assert quote.lines[0].subtotal == inr(16633 * 13)


class TestFreeDeliveryThresholdToThePaisa:
    def test_one_paisa_below_the_threshold_still_pays_delivery(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(OIL, 3)], store=store).require()
        assert quote.items_subtotal == inr(49899)
        assert quote.items_subtotal == DEFAULT_FEE_POLICY.free_delivery_threshold - inr(1)
        assert not quote.free_delivery_applied
        assert quote.delivery_fee == inr(2500)
        assert quote.gap_to_free_delivery == inr(1)

    def test_exactly_on_the_threshold_qualifies(self, store: MerchantStore) -> None:
        # Inclusive boundary: the advertised number must be reachable.
        quote = quote_basket([BasketLine(RICE, 1)], store=store).require()
        assert quote.items_subtotal == DEFAULT_FEE_POLICY.free_delivery_threshold
        assert quote.free_delivery_applied
        assert quote.delivery_fee.is_zero
        assert quote.delivery_tax.is_zero
        assert quote.gap_to_free_delivery.is_zero

    def test_above_the_threshold_qualifies(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(RICE, 1), BasketLine(MILK, 1)], store=store).require()
        assert quote.items_subtotal > DEFAULT_FEE_POLICY.free_delivery_threshold
        assert quote.free_delivery_applied

    def test_threshold_is_evaluated_pre_tax(self, store: MerchantStore) -> None:
        # 49899 pre-tax with 2495 of GST is 52394 post-tax, comfortably over Rs 499. If the
        # threshold counted tax, this basket would get free delivery and a buyer adding up
        # the shelf prices they saw could not predict the outcome.
        quote = quote_basket([BasketLine(OIL, 3)], store=store).require()
        assert quote.items_subtotal + quote.items_tax > DEFAULT_FEE_POLICY.free_delivery_threshold
        assert not quote.free_delivery_applied

    def test_policy_boundary_directly(self) -> None:
        policy = DEFAULT_FEE_POLICY
        assert not policy.qualifies_for_free_delivery(inr(49899))
        assert policy.qualifies_for_free_delivery(inr(49900))
        assert policy.qualifies_for_free_delivery(inr(49901))
        assert policy.delivery_fee_for(inr(49899)) == inr(2500)
        assert policy.delivery_fee_for(inr(49900)).is_zero
        assert policy.gap_to_free_delivery(inr(49899)) == inr(1)
        assert policy.gap_to_free_delivery(inr(49900)).is_zero

    def test_gap_is_computed_by_the_engine_not_the_caller(self, store: MerchantStore) -> None:
        # Specification 6.2: the fee engine computes the threshold gap and the agent only
        # phrases the nudge.
        quote = quote_basket([BasketLine(MILK, 1)], store=store).require()
        assert quote.gap_to_free_delivery == inr(49900 - 2800)

    def test_a_zero_threshold_policy_never_charges_delivery(self, store: MerchantStore) -> None:
        policy = FeePolicy(
            base_delivery_fee=inr(2500),
            free_delivery_threshold=Money.zero(INR),
            delivery_tax_bp=1800,
        )
        quote = quote_basket([BasketLine(MILK, 1)], store=store, policy=policy).require()
        assert quote.free_delivery_applied
        assert quote.delivery_fee.is_zero

    def test_a_zero_base_fee_prices_a_below_threshold_basket(self, store: MerchantStore) -> None:
        # A merchant who delivers free to everybody sets the base fee to zero. The basket
        # is still below the threshold, so `qualifies_for_free_delivery` is False -- but
        # nothing is charged, and Quote requires the gap to be zero whenever no delivery
        # fee is charged. Deriving the gap from the threshold alone made this basket
        # unquotable: quote_basket raised ValueError instead of returning a quote.
        policy = FeePolicy(
            base_delivery_fee=Money.zero(INR),
            free_delivery_threshold=inr(49900),
            delivery_tax_bp=1800,
        )
        quote = quote_basket([BasketLine(MILK, 1)], store=store, policy=policy).require()
        assert quote.delivery_fee.is_zero
        assert quote.delivery_tax.is_zero
        assert quote.free_delivery_applied
        # No nudge: there is nothing to spend more to reach.
        assert quote.gap_to_free_delivery.is_zero
        assert quote.total == inr(2800)

    def test_a_zero_base_fee_injected_mid_demo_still_prices(self, store: MerchantStore) -> None:
        # Same defect reached through the only mutator, which permits a zero fee.
        ScenarioController(store).set_delivery_fee(Money.zero(INR))
        quote = quote_basket([BasketLine(MILK, 1)], store=store).require()
        assert quote.free_delivery_applied
        assert quote.gap_to_free_delivery.is_zero


class TestRefusals:
    def test_unknown_sku_raises(self, store: MerchantStore) -> None:
        with pytest.raises(UnknownSkuError):
            quote_basket([BasketLine("GRO-FAKE-999", 1)], store=store)

    def test_empty_basket_and_bad_quantities_are_refused(self, store: MerchantStore) -> None:
        with pytest.raises(InvalidBasketError):
            quote_basket([], store=store)
        with pytest.raises(InvalidBasketError, match="positive"):
            BasketLine(MILK, 0)
        with pytest.raises(InvalidBasketError, match="positive"):
            BasketLine(MILK, -3)
        with pytest.raises(InvalidBasketError, match="must be an int"):
            # bool is an int subclass in Python; a True quantity must not become 1 unit.
            BasketLine(MILK, True)

    def test_duplicate_sku_is_refused_rather_than_summed(self, store: MerchantStore) -> None:
        # The kernel derives reserved units from the checkout lines; two lines for one SKU
        # make the reserved quantity ambiguous.
        with pytest.raises(InvalidBasketError, match="twice"):
            quote_basket([BasketLine(MILK, 1), BasketLine(MILK, 2)], store=store)

    def test_out_of_stock_returns_a_recovery_code_not_a_partial_quote(
        self, store: MerchantStore
    ) -> None:
        ScenarioController(store).sell_out(MILK)
        result = quote_basket([BasketLine(MILK, 1), BasketLine(DAHI, 1)], store=store)
        assert not result.ok
        assert result.code is RecoveryCode.STALE_CHECKOUT
        assert result.quote is None
        # Pricing the remainder would hand the buyer a total for a basket they never asked
        # for, and approving it would bind consent to it.
        assert [item.sku for item in result.unavailable] == [MILK]
        assert result.unavailable[0].requested == 1
        assert result.unavailable[0].available_units == 0

    def test_insufficient_stock_is_refused_at_the_exact_unit(self, store: MerchantStore) -> None:
        ScenarioController(store).set_stock(MILK, 4)
        assert quote_basket([BasketLine(MILK, 4)], store=store).ok
        refused = quote_basket([BasketLine(MILK, 5)], store=store)
        assert not refused.ok
        assert refused.unavailable[0].available_units == 4

    def test_delisted_item_is_refused_even_with_stock_on_the_shelf(
        self, store: MerchantStore
    ) -> None:
        ScenarioController(store).make_unavailable(MILK)
        assert store.get_product(MILK).stock_units > 0
        result = quote_basket([BasketLine(MILK, 1)], store=store)
        assert not result.ok
        assert result.code is RecoveryCode.STALE_CHECKOUT

    def test_quote_result_cannot_claim_ok_without_a_quote(self) -> None:
        with pytest.raises(ValueError, match="exactly when the code is OK"):
            QuoteResult(code=RecoveryCode.OK)
        with pytest.raises(ValueError, match="must name the lines"):
            QuoteResult(code=RecoveryCode.STALE_CHECKOUT)

    def test_require_raises_on_a_refusal(self, store: MerchantStore) -> None:
        ScenarioController(store).sell_out(MILK)
        with pytest.raises(InvalidBasketError):
            quote_basket([BasketLine(MILK, 1)], store=store).require()


class TestCheckoutContentAndHash:
    def test_content_carries_the_shape_the_kernel_reads(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(MILK, 2), BasketLine(DAHI, 1)], store=store).require()
        content = quote.to_checkout_content()
        assert [line["sku"] for line in content["lines"]] == [MILK, DAHI]
        assert [line["quantity"] for line in content["lines"]] == [2, 1]
        assert content["total_minor"] == quote.total.minor
        assert content["currency"] == INR

    def test_content_hashes_under_the_integer_only_profile(self, store: MerchantStore) -> None:
        # Any float anywhere in the content would raise CanonicalizationError here.
        quote = quote_basket([BasketLine(MILK, 2), BasketLine(OIL, 1)], store=store).require()
        assert quote.content_hash() == canonical_hash(quote.to_checkout_content())

    def test_hash_is_stable_across_re_quotes_of_an_unchanged_basket(
        self, store: MerchantStore
    ) -> None:
        # Two identical baskets priced against the same catalogue revision are the same
        # checkout. If the hash moved, every re-read would look like a material change and
        # demand a fresh approval.
        lines = [BasketLine(MILK, 2), BasketLine(DAHI, 1)]
        first = quote_basket(lines, store=store).require()
        second = quote_basket(lines, store=store).require()
        assert first.content_hash() == second.content_hash()

    def test_hash_moves_when_a_price_moves(self, store: MerchantStore) -> None:
        lines = [BasketLine(MILK, 2), BasketLine(DAHI, 1)]
        before = quote_basket(lines, store=store).require()
        ScenarioController(store).set_price(MILK, inr(3100))
        after = quote_basket(lines, store=store).require()
        assert after.content_hash() != before.content_hash()
        assert after.total != before.total

    def test_hash_moves_when_a_quantity_moves(self, store: MerchantStore) -> None:
        one = quote_basket([BasketLine(MILK, 1)], store=store).require()
        two = quote_basket([BasketLine(MILK, 2)], store=store).require()
        assert one.content_hash() != two.content_hash()


class TestGroundingOfQuotes:
    def test_quote_carries_source_and_revision(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(MILK, 1)], store=store).require()
        assert quote.freshness.catalogue_revision == store.revision
        assert not store.is_stale(quote.freshness)

    def test_quote_becomes_stale_after_any_injection(self, store: MerchantStore) -> None:
        quote = quote_basket([BasketLine(MILK, 1)], store=store).require()
        ScenarioController(store).set_delivery_fee(inr(4000))
        assert store.is_stale(quote.freshness)

    def test_the_wall_clock_changes_no_decision(self) -> None:
        # observed_at is descriptive metadata. A store whose clock is stuck in 1999 must
        # produce the same prices, the same fee and the same total as one that is current.
        def ancient() -> datetime:
            return datetime(1999, 1, 1, tzinfo=UTC)

        lines = [BasketLine(MILK, 2), BasketLine(OIL, 3)]
        old = quote_basket(lines, store=MerchantStore(clock=ancient)).require()
        new = quote_basket(lines, store=MerchantStore(clock=frozen_clock)).require()
        assert old.total == new.total
        assert old.delivery_fee == new.delivery_fee
        assert old.content_hash() == new.content_hash()
