"""One merchant offer, priced.

The offer is deliberately the smallest thing that is still real: one cart-wide percentage
or flat amount, with a window. What these tests pin is not the arithmetic alone but the
two decisions that make the arithmetic safe -- that pricing never asks the clock, and that
an offer can never take a cart to nothing.
"""

from __future__ import annotations

import uuid

import pytest
from commerce_domain import Money
from merchant_sim import BasketLine
from merchant_sim.fees import quote_basket
from merchant_sim.kernel_adapter import content_from_quote, receipt_inputs_for
from merchant_sim.policy import Promotion
from merchant_sim.scenarios import ScenarioController, ScenarioError
from merchant_sim.store import MerchantStore

RICE = "INDI-STPL-001"
MILK = "AMUL-DAIRY-001"

FROM_MS = 1_700_000_000_000
TO_MS = 1_893_456_000_000


@pytest.fixture
def store() -> MerchantStore:
    return MerchantStore()


@pytest.fixture
def controller(store: MerchantStore) -> ScenarioController:
    return ScenarioController(store)


def cart() -> list[BasketLine]:
    return [BasketLine(sku=RICE, quantity=1), BasketLine(sku=MILK, quantity=2)]


def start_ten_percent(controller: ScenarioController) -> None:
    controller.start_offer(
        offer_id="weekend",
        label="Weekend 10% off",
        percent_bp=1000,
        effective_from_epoch_ms=FROM_MS,
        effective_to_epoch_ms=TO_MS,
    )


class TestThePromotionValue:
    def test_a_percentage_rounds_half_up_on_whole_paise(self) -> None:
        offer = Promotion(
            offer_id="o",
            label="l",
            percent_bp=1000,
            effective_from_epoch_ms=FROM_MS,
            effective_to_epoch_ms=TO_MS,
        )
        # 5 % of 1 235 is 123.5, which must land on 124 rather than on a float.
        got = offer.discount_on(Money(1235, "INR"), payable_before_discount=Money(9999, "INR"))
        assert got == Money(124, "INR")

    def test_it_is_either_a_percentage_or_a_flat_amount(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            Promotion(
                offer_id="o",
                label="l",
                percent_bp=1000,
                flat=Money(100, "INR"),
                effective_from_epoch_ms=FROM_MS,
                effective_to_epoch_ms=TO_MS,
            )
        with pytest.raises(ValueError, match="not both"):
            Promotion(
                offer_id="o",
                label="l",
                effective_from_epoch_ms=FROM_MS,
                effective_to_epoch_ms=TO_MS,
            )

    def test_it_must_end_after_it_starts(self) -> None:
        with pytest.raises(ValueError, match="end after it starts"):
            Promotion(
                offer_id="o",
                label="l",
                percent_bp=1000,
                effective_from_epoch_ms=TO_MS,
                effective_to_epoch_ms=FROM_MS,
            )

    def test_it_never_takes_a_cart_to_nothing(self) -> None:
        """The bound that is easy to miss.

        A cart of zero-tax items above the free-delivery threshold has no tax and no fee
        to absorb the difference, so a hundred-percent offer would total exactly zero. No
        provider accepts an order for no money, and the buyer would have approved a
        purchase that cannot be executed.
        """
        everything = Promotion(
            offer_id="all",
            label="Everything free",
            percent_bp=10_000,
            effective_from_epoch_ms=FROM_MS,
            effective_to_epoch_ms=TO_MS,
        )
        got = everything.discount_on(
            Money(51_100, "INR"), payable_before_discount=Money(51_100, "INR")
        )
        assert got == Money(51_099, "INR"), "one paisa must survive"

    def test_a_flat_offer_cannot_exceed_the_goods(self) -> None:
        big = Promotion(
            offer_id="big",
            label="₹5000 off",
            flat=Money(500_000, "INR"),
            effective_from_epoch_ms=FROM_MS,
            effective_to_epoch_ms=TO_MS,
        )
        got = big.discount_on(Money(55_500, "INR"), payable_before_discount=Money(57_995, "INR"))
        assert got == Money(55_500, "INR")


class TestPricing:
    def test_a_quote_with_no_offer_is_unchanged(self, store: MerchantStore) -> None:
        quote = quote_basket(cart(), store=store).require()
        assert quote.discount_amount == Money(0, "INR")
        assert quote.offer_label is None
        assert quote.total == Money(57_995, "INR")

    def test_the_offer_comes_off_the_total(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        before = quote_basket(cart(), store=store).require()
        start_ten_percent(controller)
        after = quote_basket(cart(), store=store).require()
        assert after.discount_amount == Money(5_550, "INR")
        assert after.offer_label == "Weekend 10% off"
        assert after.offer_valid_till_epoch_ms == TO_MS
        # The saving printed beside the offer is the difference between the two totals a
        # buyer can see. That is the whole reason it is applied last.
        assert before.total.minor - after.total.minor == after.discount_amount.minor

    def test_ending_the_offer_restores_the_price(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        start_ten_percent(controller)
        controller.end_offer()
        assert quote_basket(cart(), store=store).require().total == Money(57_995, "INR")

    def test_a_reset_clears_the_offer(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        start_ten_percent(controller)
        controller.reset()
        assert store.promotion is None
        assert quote_basket(cart(), store=store).require().discount_amount.is_zero

    def test_two_quotes_at_one_revision_price_identically(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        """Why the window does not gate the pricing.

        This package's grounding rule is that nothing may decide anything from the clock.
        If an offer expired between two reads of one catalogue revision, the same cart
        would carry two totals and neither could be proven stale by the revision.
        """
        start_ten_percent(controller)
        first = quote_basket(cart(), store=store).require()
        second = quote_basket(cart(), store=store).require()
        assert first.total == second.total
        assert first.content_hash() == second.content_hash()


class TestTheLevers:
    def test_only_one_offer_runs_at_a_time(self, controller: ScenarioController) -> None:
        start_ten_percent(controller)
        with pytest.raises(ScenarioError, match="already running"):
            controller.start_offer(
                offer_id="another",
                label="Another",
                percent_bp=500,
                effective_from_epoch_ms=FROM_MS,
                effective_to_epoch_ms=TO_MS,
            )

    def test_ending_nothing_is_refused(self, controller: ScenarioController) -> None:
        """An injection that changes no state would still advance the revision."""
        with pytest.raises(ScenarioError, match="no offer is running"):
            controller.end_offer()

    def test_starting_an_offer_advances_the_revision(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        before = store.revision
        injection = start_ten_percent(controller) or store.injections[-1]
        assert store.revision == before + 1
        assert injection.kind.value == "OFFER_START"
        assert injection.delta.field == "offer_value"
        assert injection.delta.after == 1000


class TestWhatTheKernelIsTold:
    def test_the_discount_reaches_the_checkout_content(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        start_ten_percent(controller)
        quote = quote_basket(cart(), store=store).require()
        content = content_from_quote(
            quote, checkout_id=uuid.uuid4(), version=1, policy_version="sim-1"
        )
        assert content["discount_minor"] == 5_550
        # The kernel validates subtotal + tax + delivery - discount itself, so a quote and
        # the content built from it cannot disagree about what an offer was worth.
        assert (
            content["subtotal_minor"]
            + content["tax_minor"]
            + content["delivery_fee_minor"]
            - content["discount_minor"]
            == content["total_minor"]
        )

    def test_the_receipt_records_the_offer_as_a_rule_of_its_own(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        start_ten_percent(controller)
        discount = _discount_policy(store)
        assert discount.policy_id.endswith("/discount/weekend")
        assert discount.terms["allowed"] is True
        assert discount.terms["label"] == "Weekend 10% off"
        assert discount.terms["percent_bp"] == 1000
        assert discount.terms["effective_to_epoch_ms"] == TO_MS
        # The settled rule: a refund returns what was paid, and the offer's value expires
        # rather than becoming cash.
        assert discount.terms["refund_basis"] == "PAID_AMOUNT"
        assert discount.terms["credit_expires_on_refund"] is True

    def test_with_no_offer_the_receipt_still_says_so_explicitly(self, store: MerchantStore) -> None:
        """An omitted kind would be filled in later from current policy."""
        assert _discount_policy(store).terms == {"allowed": False}

    def test_two_offers_are_two_distinguishable_rules(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        start_ten_percent(controller)
        first = _discount_policy(store).policy_id
        controller.end_offer()
        controller.start_offer(
            offer_id="diwali",
            label="Diwali ₹50 off",
            flat=Money(5_000, "INR"),
            effective_from_epoch_ms=FROM_MS,
            effective_to_epoch_ms=TO_MS,
        )
        second = _discount_policy(store)
        assert second.policy_id != first
        assert second.terms["kind"] == "FLAT"
        assert second.terms["flat_minor"] == 5_000


def _discount_policy(store: MerchantStore):  # type: ignore[no-untyped-def]
    return next(p for p in receipt_inputs_for(store).policies if p.kind.value == "DISCOUNT")
