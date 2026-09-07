"""The scenario controller: labelled injections and the version N -> N+1 demo story.

The claim being defended is specification 31.3: every injected change is labelled
``SCENARIO_INJECTION`` and never mixed with organic data. These tests check that the label
is structural -- that there is no reachable code path which changes merchant state without
producing a labelled, logged record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from merchant_sim.errors import ScenarioError
from merchant_sim.fees import BasketLine, quote_basket
from merchant_sim.injection import (
    SCENARIO_LABEL,
    InjectionKind,
    ScenarioInjection,
    StateDelta,
)
from merchant_sim.policy import DEFAULT_FEE_POLICY
from merchant_sim.scenarios import ScenarioController
from merchant_sim.search import search
from merchant_sim.store import MerchantStore

INR = "INR"
MILK = "AMUL-DAIRY-001"  # 2800 paise, 48 units
ATTA = "AASH-STPL-002"  # 25500 paise, 18 units
DAHI = "AMUL-DAIRY-003"  # 4500 paise, 24 units


def frozen_clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def inr(minor: int) -> Money:
    return Money(minor, INR)


@pytest.fixture
def store() -> MerchantStore:
    return MerchantStore(clock=frozen_clock)


@pytest.fixture
def controller(store: MerchantStore) -> ScenarioController:
    return ScenarioController(store)


class TestLabellingIsStructural:
    def test_the_label_cannot_be_anything_else(self) -> None:
        # An unlabelled demo change is indistinguishable from an organic merchant failure.
        # During a live demo that is the difference between "we injected this" and "our
        # inventory service just broke".
        with pytest.raises(ScenarioError, match=SCENARIO_LABEL):
            ScenarioInjection(
                injection_id=uuid7(),
                kind=InjectionKind.STOCK_SET,
                deltas=(StateDelta(field="stock_units", before=48, after=0),),
                revision_before=0,
                revision_after=1,
                injected_at=frozen_clock(),
                sku=MILK,
                label="ROUTINE_UPDATE",
            )

    def test_every_controller_method_produces_a_labelled_log_entry(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        controller.set_stock(MILK, 5)
        controller.decrement_stock(MILK, 2)
        controller.set_price(ATTA, inr(28500))
        controller.make_unavailable(DAHI)
        controller.make_available(DAHI)
        controller.set_delivery_fee(inr(4000))
        controller.set_free_delivery_threshold(inr(60000))
        controller.start_offer(
            offer_id="weekend",
            label="Weekend 10% off",
            percent_bp=1000,
            effective_from_epoch_ms=1_700_000_000_000,
            effective_to_epoch_ms=1_700_600_000_000,
        )
        controller.end_offer()
        controller.reset()

        assert len(store.injections) == 10
        assert all(item.label == SCENARIO_LABEL for item in store.injections)
        assert {item.kind for item in store.injections} == set(InjectionKind)

    def test_the_store_exposes_no_other_mutator(self, store: MerchantStore) -> None:
        # If a new public mutator is added without going through mutate(), this fails and
        # forces the author to justify an unlabelled path.
        public = {
            name
            for name in dir(store)
            if not name.startswith("_") and callable(getattr(store, name))
        }
        assert public == {
            "all_skus",
            "check_inventory",
            "freshness",
            "get_product",
            "is_listed",
            "is_stale",
            "mutate",
        }

    def test_a_refused_injection_changes_nothing(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        before_stock = store.check_inventory(MILK).available_units
        before_revision = store.revision
        with pytest.raises(ScenarioError):
            controller.decrement_stock(MILK, before_stock + 1)
        assert store.check_inventory(MILK).available_units == before_stock
        assert store.revision == before_revision
        assert store.injections == ()


class TestRevisionDiscipline:
    def test_revision_advances_by_exactly_one_per_injection(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        assert store.revision == 0
        for expected in range(1, 4):
            controller.decrement_stock(MILK, 1)
            assert store.revision == expected

    def test_an_injection_built_against_a_stale_revision_is_refused(
        self, store: MerchantStore
    ) -> None:
        # Two controllers racing must not interleave silently: the log would stop being a
        # replayable total order over merchant state.
        stale = ScenarioInjection(
            injection_id=uuid7(),
            kind=InjectionKind.STOCK_SET,
            deltas=(StateDelta(field="stock_units", before=48, after=10),),
            revision_before=0,
            revision_after=1,
            injected_at=frozen_clock(),
            sku=MILK,
        )
        ScenarioController(store).set_stock(MILK, 20)  # store moves to revision 1
        with pytest.raises(ScenarioError, match="revision"):
            store.mutate(stale)
        assert store.check_inventory(MILK).available_units == 20

    def test_an_injection_may_not_skip_a_revision(self) -> None:
        with pytest.raises(ScenarioError, match="exactly one"):
            ScenarioInjection(
                injection_id=uuid7(),
                kind=InjectionKind.STOCK_SET,
                deltas=(StateDelta(field="stock_units", before=48, after=0),),
                revision_before=0,
                revision_after=2,
                injected_at=frozen_clock(),
                sku=MILK,
            )

    def test_compare_and_set_rejects_a_stale_before_value(self, store: MerchantStore) -> None:
        # The operator built this from a screen showing 48 units; state has since moved.
        # Applying `after` blindly would overwrite a change nobody meant to discard.
        ScenarioController(store).set_stock(MILK, 7)
        wrong = ScenarioInjection(
            injection_id=uuid7(),
            kind=InjectionKind.STOCK_SET,
            deltas=(StateDelta(field="stock_units", before=48, after=0),),
            revision_before=store.revision,
            revision_after=store.revision + 1,
            injected_at=frozen_clock(),
            sku=MILK,
        )
        with pytest.raises(ScenarioError, match="expected current value 48"):
            store.mutate(wrong)
        assert store.check_inventory(MILK).available_units == 7


class TestInjectionShapeValidation:
    def test_a_kind_may_only_change_its_own_field(self) -> None:
        with pytest.raises(ScenarioError, match="may only change"):
            ScenarioInjection(
                injection_id=uuid7(),
                kind=InjectionKind.PRICE_SET,
                deltas=(StateDelta(field="stock_units", before=48, after=0),),
                revision_before=0,
                revision_after=1,
                injected_at=frozen_clock(),
                sku=MILK,
            )

    def test_sku_scoped_and_store_wide_kinds_are_kept_apart(self) -> None:
        with pytest.raises(ScenarioError, match="must name the SKU"):
            ScenarioInjection(
                injection_id=uuid7(),
                kind=InjectionKind.STOCK_SET,
                deltas=(StateDelta(field="stock_units", before=48, after=0),),
                revision_before=0,
                revision_after=1,
                injected_at=frozen_clock(),
            )
        with pytest.raises(ScenarioError, match="store-wide"):
            ScenarioInjection(
                injection_id=uuid7(),
                kind=InjectionKind.CATALOGUE_RESET,
                deltas=(),
                revision_before=0,
                revision_after=1,
                injected_at=frozen_clock(),
                sku=MILK,
            )

    def test_a_delta_cannot_change_type_between_before_and_after(self) -> None:
        with pytest.raises(ScenarioError, match="same type"):
            StateDelta(field="available", before=True, after=3)

    def test_availability_must_be_a_boolean_at_apply_time(self, store: MerchantStore) -> None:
        bad = ScenarioInjection(
            injection_id=uuid7(),
            kind=InjectionKind.AVAILABILITY_SET,
            deltas=(StateDelta(field="available", before=1, after=0),),
            revision_before=0,
            revision_after=1,
            injected_at=frozen_clock(),
            sku=MILK,
        )
        with pytest.raises(ScenarioError, match="boolean"):
            store.mutate(bad)

    def test_an_injection_may_not_re_denominate_a_sku(self, store: MerchantStore) -> None:
        # The controller guards this, but mutate() is the documented single entry point
        # and must not be weaker than its only caller: a SKU priced in another currency
        # leaves the store in a state the fee engine cannot price at all.
        bad = ScenarioInjection(
            injection_id=uuid7(),
            kind=InjectionKind.PRICE_SET,
            deltas=(StateDelta(field="unit_price_minor", before=2800, after=2800),),
            revision_before=0,
            revision_after=1,
            injected_at=frozen_clock(),
            sku=MILK,
            currency="USD",
        )
        with pytest.raises(ScenarioError, match="re-denominate"):
            store.mutate(bad)
        assert store.get_product(MILK).unit_price == inr(2800)
        assert store.revision == 0
        assert store.injections == ()

    def test_a_negative_fee_is_refused_by_the_store_not_only_the_controller(
        self, store: MerchantStore
    ) -> None:
        bad = ScenarioInjection(
            injection_id=uuid7(),
            kind=InjectionKind.DELIVERY_FEE_SET,
            deltas=(StateDelta(field="base_delivery_fee_minor", before=2500, after=-100),),
            revision_before=0,
            revision_after=1,
            injected_at=frozen_clock(),
        )
        with pytest.raises(ScenarioError, match="discount"):
            store.mutate(bad)
        assert store.fee_policy == DEFAULT_FEE_POLICY
        assert store.revision == 0

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(ScenarioError, match="timezone-aware"):
            ScenarioInjection(
                injection_id=uuid7(),
                kind=InjectionKind.CATALOGUE_RESET,
                deltas=(),
                revision_before=0,
                revision_after=1,
                injected_at=datetime(2026, 9, 4, 12, 0, 0),  # noqa: DTZ001
            )


class TestControllerGuards:
    def test_stock_cannot_go_negative(self, controller: ScenarioController) -> None:
        with pytest.raises(ScenarioError, match="negative"):
            controller.set_stock(MILK, -1)

    def test_decrement_refuses_to_take_more_than_is_there(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        # Clamping to zero would hide the interesting case: a cart losing a race for the
        # last unit is a demo scenario, a store inventing a floor is a bug.
        on_hand = store.check_inventory(MILK).available_units
        with pytest.raises(ScenarioError, match="only 48 on hand"):
            controller.decrement_stock(MILK, on_hand + 1)

    def test_no_op_injections_are_refused(self, controller: ScenarioController) -> None:
        # An injection that changes nothing would still advance the revision and make every
        # outstanding quote stale for no reason.
        with pytest.raises(ScenarioError, match="no-op"):
            controller.set_price(MILK, inr(2800))
        with pytest.raises(ScenarioError, match="no-op"):
            controller.set_delivery_fee(DEFAULT_FEE_POLICY.base_delivery_fee)
        with pytest.raises(ScenarioError, match="already listed"):
            controller.make_available(MILK)

    def test_price_must_stay_positive_and_in_currency(self, controller: ScenarioController) -> None:
        with pytest.raises(ScenarioError, match="positive"):
            controller.set_price(MILK, inr(0))
        with pytest.raises(ScenarioError, match="cannot set USD"):
            controller.set_price(MILK, Money(2800, "USD"))

    def test_selling_out_and_delisting_are_distinguishable(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        # "we ran out" and "we stopped selling it" lead to different substitution advice.
        controller.sell_out(MILK)
        assert store.is_listed(MILK)
        assert store.check_inventory(MILK).available_units == 0

        controller.make_unavailable(DAHI)
        assert not store.is_listed(DAHI)
        assert store.get_product(DAHI).stock_units > 0
        assert not store.check_inventory(DAHI).is_available


class TestResetAndAudit:
    def test_reset_restores_every_price_stock_listing_and_fee(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        baseline = {sku: store.get_product(sku) for sku in store.all_skus()}
        controller.set_price(MILK, inr(9999))
        controller.set_stock(ATTA, 0)
        controller.make_unavailable(DAHI)
        controller.set_delivery_fee(inr(9900))
        controller.reset()

        for sku, before in baseline.items():
            now = store.get_product(sku)
            assert now.unit_price == before.unit_price, sku
            assert now.stock_units == before.stock_units, sku
            assert now.is_listed == before.is_listed, sku
        assert store.fee_policy == DEFAULT_FEE_POLICY

    def test_reset_still_advances_the_revision(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        # A quote taken before the reset was priced against a world that no longer exists,
        # so it must read as stale even though the numbers happen to match again.
        controller.set_price(MILK, inr(9999))
        quote = quote_basket([BasketLine(DAHI, 1)], store=store).require()
        controller.reset()
        assert store.is_stale(quote.freshness)

    def test_audit_payload_is_canonically_hashable(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        injection = controller.set_price(MILK, inr(3100))
        payload = injection.to_audit_payload()
        assert payload["label"] == SCENARIO_LABEL
        assert payload["kind"] == InjectionKind.PRICE_SET.value
        assert payload["deltas"] == [{"field": "unit_price_minor", "before": 2800, "after": 3100}]
        # Would raise CanonicalizationError if any float reached the payload.
        assert canonical_hash(payload) == canonical_hash(injection.to_audit_payload())

    def test_injection_ids_are_unique_and_time_ordered(
        self, controller: ScenarioController
    ) -> None:
        ids = [controller.decrement_stock(MILK, 1).injection_id for _ in range(5)]
        assert len(set(ids)) == 5
        assert all(isinstance(value, uuid.UUID) and value.version == 7 for value in ids)


class TestVersionNToNPlusOneStory:
    """Specification 31.1, steps 5-9: the demo's material-delta moment."""

    def test_a_price_rise_and_a_stock_out_invalidate_version_n(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        cart = [BasketLine(MILK, 2), BasketLine(ATTA, 1), BasketLine(DAHI, 1)]

        # --- version N: quoted and (in the real flow) approved -------------------
        version_n = quote_basket(cart, store=store).require()
        assert version_n.total == inr(40050)
        assert not store.is_stale(version_n.freshness)
        hash_n = version_n.content_hash()

        # --- the scenario controller changes the world --------------------------
        controller.set_price(ATTA, inr(28500), note="supplier price rise")
        controller.sell_out(MILK, note="last cartons taken by another buyer")
        assert all(item.label == SCENARIO_LABEL for item in store.injections)

        # --- version N is now provably stale ------------------------------------
        assert store.is_stale(version_n.freshness)
        refused = quote_basket(cart, store=store)
        assert not refused.ok
        assert [item.sku for item in refused.unavailable] == [MILK]

        # --- version N+1: a different cart, a different total, a different hash --
        version_n1 = quote_basket([BasketLine(ATTA, 1), BasketLine(DAHI, 1)], store=store)
        quote_n1 = version_n1.require()
        assert quote_n1.total == inr(37600)
        assert quote_n1.content_hash() != hash_n
        assert quote_n1.freshness.catalogue_revision == store.revision
        # Fresh consent is required because the amount the buyer approved no longer holds.
        assert quote_n1.total != version_n.total

    def test_the_injected_deltas_explain_the_change_exactly(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        controller.set_price(ATTA, inr(28500))
        controller.sell_out(MILK)
        price_change, stock_change = store.injections
        assert price_change.delta.before == 25500
        assert price_change.delta.after == 28500
        assert stock_change.delta.before == 48
        assert stock_change.delta.after == 0
        # An operator can replay the demo from the log alone.
        assert [item.revision_before for item in store.injections] == [0, 1]
        assert [item.revision_after for item in store.injections] == [1, 2]

    def test_a_fee_change_alone_moves_the_total_and_the_hash(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        cart = [BasketLine(DAHI, 1)]
        before = quote_basket(cart, store=store).require()
        controller.set_delivery_fee(inr(5000))
        after = quote_basket(cart, store=store).require()
        assert after.delivery_fee == inr(5000)
        assert after.total == before.total + inr(2500) + inr(450)
        assert after.content_hash() != before.content_hash()

    def test_a_threshold_change_can_flip_free_delivery_mid_basket(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        cart = [BasketLine(ATTA, 1)]  # 25500 paise: below the Rs 499 threshold
        assert not quote_basket(cart, store=store).require().free_delivery_applied
        controller.set_free_delivery_threshold(inr(20000))
        assert quote_basket(cart, store=store).require().free_delivery_applied

    def test_search_reflects_an_injection_immediately(
        self, store: MerchantStore, controller: ScenarioController
    ) -> None:
        controller.set_price(MILK, inr(3100))
        hit = next(h for h in search("doodh", store=store).hits if h.sku == MILK)
        assert hit.view.unit_price == inr(3100)
        assert hit.view.freshness.catalogue_revision == store.revision
