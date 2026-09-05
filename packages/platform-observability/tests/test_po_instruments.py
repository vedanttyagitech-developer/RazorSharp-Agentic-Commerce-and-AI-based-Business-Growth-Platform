"""The catalogue is data, and data can be checked.

"Counters that are never surfaced are not observability" (specification 19.13) is a rule
about what a catalogue must contain, so it is asserted here rather than trusted: every
instrument has a description, names a decision, renders even at zero, and follows the
naming conventions a Prometheus consumer relies on.
"""

from __future__ import annotations

import pytest
from platform_observability import (
    ADMISSION_TIMING,
    PLATFORM_INSTRUMENTS,
    PROVIDER_TIMING,
    SELF_INSTRUMENTS,
    WORKER_COMMAND_TIMING,
    InstrumentKind,
    InstrumentSpec,
    MetricsRegistry,
    Timing,
    default_registry,
    reset_default_registry,
)

ALL_SPECS = (*PLATFORM_INSTRUMENTS, *SELF_INSTRUMENTS)


@pytest.fixture(autouse=True)
def _clean_default() -> None:
    """The process-wide registry is a singleton; a test must not inherit another's."""
    reset_default_registry()


class TestCatalogueIntegrity:
    def test_names_are_unique(self) -> None:
        names = [spec.name for spec in ALL_SPECS]
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.name)
    def test_every_instrument_names_a_decision(self, spec: InstrumentSpec) -> None:
        """The field that makes this a catalogue rather than a list of names. An
        instrument nobody can name a decision for gets surfaced on a dashboard nobody
        reads, because nobody knows what seeing it should make them do."""
        assert len(spec.decision.split()) >= 5, spec.name

    @pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.name)
    def test_every_instrument_has_a_sentence_of_help(self, spec: InstrumentSpec) -> None:
        assert spec.description.endswith("."), spec.name
        assert len(spec.description.split()) >= 4, spec.name

    @pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.name)
    def test_naming_conventions_hold(self, spec: InstrumentSpec) -> None:
        """A Prometheus consumer reads meaning off a name: ``_total`` is a counter and
        ``_seconds`` is a duration in seconds, not in milliseconds."""
        assert spec.name.endswith("_total") is (spec.kind is InstrumentKind.COUNTER), spec.name
        if spec.kind is InstrumentKind.HISTOGRAM:
            assert spec.name.endswith("_seconds"), spec.name
            assert spec.buckets, spec.name

    @pytest.mark.parametrize("spec", PLATFORM_INSTRUMENTS, ids=lambda spec: spec.name)
    def test_platform_instruments_are_tenant_scoped_and_namespaced(
        self, spec: InstrumentSpec
    ) -> None:
        """Every fact about this platform is a fact about one tenant, exactly as every row
        in the database is."""
        assert spec.tenant_scoped, spec.name
        assert spec.name.startswith("commerce_"), spec.name

    @pytest.mark.parametrize("spec", SELF_INSTRUMENTS, ids=lambda spec: spec.name)
    def test_the_registry_s_own_instruments_are_not_tenant_scoped(
        self, spec: InstrumentSpec
    ) -> None:
        """A drop is a fact about this process, and attributing one to a tenant would need
        the very tenant label the drop is usually about not having."""
        assert not spec.tenant_scoped, spec.name
        assert spec.name.startswith("observability_"), spec.name

    @pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.name)
    def test_no_label_could_carry_an_identifier(self, spec: InstrumentSpec) -> None:
        """A label holding a checkout id, an order id or a buyer reference is unbounded
        cardinality *and* a buyer identifier in an exported series that outlives the
        request. Every label here is an enum member or a route template.
        """
        forbidden = ("_id", "buyer", "email", "checkout", "order_", "payment_", "refund_")
        for label in spec.labels:
            assert not any(fragment in label for fragment in forbidden), (spec.name, label)


class TestCoverageOfWhatThePlatformDoes:
    """The catalogue has to cover the places the system decides or waits."""

    @pytest.mark.parametrize(
        "name",
        [
            # admission and its reasons
            "commerce_admissions_total",
            "commerce_admission_denials_total",
            # grants, issued through to expired
            "commerce_execution_grants_issued_total",
            "commerce_execution_grants_consumed_total",
            "commerce_execution_grants_expired_total",
            # the outbox and the worker that drains it
            "commerce_outbox_depth",
            "commerce_worker_leases_total",
            "commerce_worker_attempts_total",
            "commerce_worker_dead_letters_total",
            # the provider
            "commerce_provider_requests_total",
            "commerce_provider_request_duration_seconds",
            # webhooks, including duplicates
            "commerce_webhook_deliveries_total",
            # reconciliation
            "commerce_reconciliation_runs_total",
            "commerce_reconciliation_findings_total",
            # refunds by wire state
            "commerce_refunds_open",
        ],
    )
    def test_the_instrument_exists(self, name: str) -> None:
        assert name in {spec.name for spec in PLATFORM_INSTRUMENTS}

    def test_webhook_duplicates_are_a_disposition_rather_than_a_second_counter(self) -> None:
        """Counted on one instrument so accepted and duplicate always sum to what the
        provider sent; two counters would drift the first time one call site was missed."""
        spec = next(
            item
            for item in PLATFORM_INSTRUMENTS
            if item.name == "commerce_webhook_deliveries_total"
        )
        assert "disposition" in spec.labels
        assert "duplicate" in spec.decision

    def test_refunds_are_a_gauge_by_wire_state_not_a_ledger(self) -> None:
        """A counter is never the record of a money action. This one says how many refunds
        are waiting; ``refunds`` and its audit chain say what happened to each."""
        spec = next(item for item in PLATFORM_INSTRUMENTS if item.name == "commerce_refunds_open")
        assert spec.kind is InstrumentKind.GAUGE
        assert spec.labels == ("wire_state",)
        assert "Neither is the refund record" in spec.decision


class TestTimings:
    @pytest.mark.parametrize("timing", [ADMISSION_TIMING, PROVIDER_TIMING, WORKER_COMMAND_TIMING])
    def test_a_timing_names_registered_instruments(self, timing: Timing) -> None:
        names = {spec.name for spec in PLATFORM_INSTRUMENTS}
        assert timing.duration in names
        if timing.outcome_counter is not None:
            assert timing.outcome_counter in names

    @pytest.mark.parametrize("timing", [PROVIDER_TIMING, WORKER_COMMAND_TIMING])
    def test_a_timing_s_outcome_label_is_declared_on_both_instruments(self, timing: Timing) -> None:
        """Otherwise the counter silently drops every recording the timing makes."""
        by_name = {spec.name: spec for spec in PLATFORM_INSTRUMENTS}
        assert timing.outcome_counter is not None
        assert timing.outcome_label in by_name[timing.outcome_counter].labels
        assert timing.outcome_label in by_name[timing.duration].labels


class TestDefaultRegistry:
    def test_the_platform_catalogue_is_registered(self) -> None:
        names = {spec.name for spec in default_registry().specs()}
        assert {spec.name for spec in PLATFORM_INSTRUMENTS} <= names
        assert {spec.name for spec in SELF_INSTRUMENTS} <= names

    def test_it_is_a_singleton_and_resettable(self) -> None:
        assert default_registry() is default_registry()
        first = default_registry()
        reset_default_registry()
        assert default_registry() is not first

    def test_every_instrument_is_surfaced_before_it_ever_fires(self) -> None:
        """Specification 19.13, mechanically. A scrape of a freshly started process lists
        every instrument with its help text, so "no data" and "not wired up" are
        distinguishable on the first day rather than the first incident."""
        text = default_registry().render()
        for spec in (*PLATFORM_INSTRUMENTS, *SELF_INSTRUMENTS):
            assert f"# TYPE {spec.name} {spec.kind.value}" in text, spec.name
            assert f"# HELP {spec.name} " in text, spec.name

    def test_a_component_may_use_its_own_registry_instead(self) -> None:
        """Nothing requires the singleton, so a second exposition -- or a test -- is free."""
        private = MetricsRegistry()
        assert private is not default_registry()
        assert {spec.name for spec in private.specs()} == {spec.name for spec in SELF_INSTRUMENTS}
