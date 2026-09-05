"""The registry: valid exposition, tenant isolation, cardinality, and never raising.

The four properties asserted here are the ones the rest of the platform is entitled to
assume:

* what :meth:`MetricsRegistry.render` produces is parseable Prometheus text, including the
  histogram invariants a scraper rejects a payload for getting wrong;
* a tenant's series cannot be written or read by code holding another tenant's handle;
* an unbounded label stops the registry rather than the process;
* nothing a caller can do to this module raises into the caller.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import pytest
from platform_observability import (
    InstrumentKind,
    InstrumentSpec,
    MetricsRegistry,
    Sample,
    SpecConflictError,
)
from platform_observability.metrics import (
    LATENCY_BUCKETS,
    MAX_SERIES_PER_INSTRUMENT,
    SINK_FAILURE_LIMIT,
    DropReason,
)

COUNTER = InstrumentSpec(
    name="test_things_total",
    kind=InstrumentKind.COUNTER,
    description="Things, by kind.",
    decision="Whether things are happening.",
    labels=("kind",),
)

GAUGE = InstrumentSpec(
    name="test_depth",
    kind=InstrumentKind.GAUGE,
    description="How deep.",
    decision="Whether the queue is draining.",
    labels=("queue",),
)

HISTOGRAM = InstrumentSpec(
    name="test_duration_seconds",
    kind=InstrumentKind.HISTOGRAM,
    description="How long.",
    decision="Whether the call is slow.",
    labels=("operation",),
    unit="seconds",
    buckets=LATENCY_BUCKETS,
)

GLOBAL_COUNTER = InstrumentSpec(
    name="test_process_events_total",
    kind=InstrumentKind.COUNTER,
    description="Process-wide events.",
    decision="Whether the process is doing anything.",
    labels=("kind",),
    tenant_scoped=False,
)


@pytest.fixture
def registry() -> MetricsRegistry:
    reg = MetricsRegistry()
    reg.register_all((COUNTER, GAUGE, HISTOGRAM, GLOBAL_COUNTER))
    return reg


# --------------------------------------------------------------- exposition


class TestPrometheusExposition:
    """What comes out has to be something a scraper will accept."""

    def test_every_line_is_a_comment_or_a_sample(self, registry: MetricsRegistry) -> None:
        registry.for_tenant("t1").increment("test_things_total", kind="a")
        registry.for_tenant("t1").observe("test_duration_seconds", 0.3, operation="submit")

        text = registry.render()
        assert text.endswith("\n"), "the exposition's last line must be terminated"
        sample = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9.eE+]+|\+Inf$")
        for line in text.splitlines():
            assert line, "a blank line is not part of the text format"
            assert line.startswith("# ") or sample.match(line), line

    def test_help_and_type_precede_every_instrument(self, registry: MetricsRegistry) -> None:
        text = registry.render()
        assert "# HELP test_things_total Things, by kind." in text
        assert "# TYPE test_things_total counter" in text
        assert "# TYPE test_depth gauge" in text
        assert "# TYPE test_duration_seconds histogram" in text

    def test_an_instrument_with_no_series_is_still_surfaced(
        self, registry: MetricsRegistry
    ) -> None:
        """Specification 19.13: a counter that is never surfaced is not observability.

        The day someone looks for an instrument is the day it has not fired, and the
        question they are answering is whether that means "healthy" or "never wired up".
        An exposition that only lists what has already happened cannot answer it.
        """
        text = registry.render()
        assert "# TYPE test_things_total counter" in text
        assert not any(line.startswith("test_things_total{") for line in text.splitlines())

    def test_help_escapes_backslash_and_newline(self) -> None:
        registry = MetricsRegistry()
        registry.register(
            InstrumentSpec(
                name="test_escaped_total",
                kind=InstrumentKind.COUNTER,
                description="A description with a \\ and a\nnewline.",
                decision="That escaping works.",
            )
        )
        help_line = next(
            line
            for line in registry.render().splitlines()
            if line.startswith("# HELP test_escaped_total")
        )
        assert "\\\\" in help_line
        assert help_line.endswith(r"newline.")
        assert r"\n" in help_line

    def test_label_values_escape_quotes_and_backslashes(self, registry: MetricsRegistry) -> None:
        registry.for_tenant('t"1\\').increment("test_things_total", kind='a"b')
        line = next(
            line for line in registry.render().splitlines() if line.startswith("test_things_total{")
        )
        assert r'tenant="t\"1\\"' in line
        assert r'kind="a\"b"' in line

    def test_histogram_buckets_are_cumulative_and_inf_equals_count(
        self, registry: MetricsRegistry
    ) -> None:
        """The ``+Inf`` bucket must equal ``_count``. A scrape where it does not is
        rejected by some consumers and silently mis-quantiled by the rest."""
        metrics = registry.for_tenant("t1")
        for value in (0.001, 0.3, 0.3, 45.0):
            metrics.observe("test_duration_seconds", value, operation="submit")

        lines = registry.render().splitlines()
        buckets = [line for line in lines if line.startswith("test_duration_seconds_bucket")]
        counts = [int(line.rsplit(" ", 1)[1]) for line in buckets]
        assert counts == sorted(counts), "bucket counts must be non-decreasing"

        inf_line = next(line for line in buckets if 'le="+Inf"' in line)
        count_line = next(line for line in lines if line.startswith("test_duration_seconds_count"))
        assert inf_line.rsplit(" ", 1)[1] == count_line.rsplit(" ", 1)[1] == "4"

        sum_line = next(line for line in lines if line.startswith("test_duration_seconds_sum"))
        assert math.isclose(float(sum_line.rsplit(" ", 1)[1]), 45.601)

    def test_a_value_above_every_bound_lands_only_in_inf(self, registry: MetricsRegistry) -> None:
        registry.for_tenant("t1").observe("test_duration_seconds", 1000.0, operation="slow")
        snapshot = registry.histogram("test_duration_seconds", tenant="t1", operation="slow")
        assert snapshot is not None
        assert {count for _, count in snapshot.buckets} == {0}
        assert snapshot.count == 1

    def test_render_is_deterministic(self, registry: MetricsRegistry) -> None:
        """Two scrapes of an unchanged registry differ in nothing, so a diff of two
        scrapes is a diff of the numbers."""
        metrics = registry.for_tenant("t1")
        for kind in ("z", "a", "m"):
            metrics.increment("test_things_total", kind=kind)
        assert registry.render() == registry.render()

    def test_counter_renders_as_an_integer(self, registry: MetricsRegistry) -> None:
        registry.for_tenant("t1").increment("test_things_total", 3, kind="a")
        assert 'test_things_total{tenant="t1",kind="a"} 3' in registry.render()


# ------------------------------------------------------------------ tenancy


class TestTenantScoping:
    """A tenant's series belongs to that tenant. Structurally, not by convention."""

    def test_two_tenants_keep_independent_series(self, registry: MetricsRegistry) -> None:
        registry.for_tenant("acme").increment("test_things_total", kind="a")
        registry.for_tenant("acme").increment("test_things_total", kind="a")
        registry.for_tenant("globex").increment("test_things_total", kind="a")

        assert registry.value("test_things_total", tenant="acme", kind="a") == 2
        assert registry.value("test_things_total", tenant="globex", kind="a") == 1

    def test_a_tenant_handle_cannot_address_another_tenant(self, registry: MetricsRegistry) -> None:
        """The tenant label is the handle's own state. Passing one is refused, not honoured.

        This is the property that makes cross-tenant leakage impossible rather than
        unlikely: there is no argument on the recording methods that reaches another
        tenant's series.
        """
        registry.for_tenant("acme").increment("test_things_total", kind="a", tenant="globex")

        assert registry.value("test_things_total", tenant="globex", kind="a") is None
        assert registry.value("test_things_total", tenant="acme", kind="a") is None
        assert (
            registry.value(
                "observability_dropped_total",
                instrument="test_things_total",
                reason=DropReason.TENANT_OVERRIDE.value,
            )
            == 1
        )

    def test_a_tenant_scoped_instrument_is_not_writable_without_a_tenant(
        self, registry: MetricsRegistry
    ) -> None:
        """Guessing which tenant a payment belonged to is worse than not counting it."""
        registry.increment("test_things_total", kind="a")

        assert registry.series("test_things_total") == ()
        assert (
            registry.value(
                "observability_dropped_total",
                instrument="test_things_total",
                reason=DropReason.MISSING_TENANT.value,
            )
            == 1
        )

    def test_every_tenant_series_carries_the_tenant_label_first(
        self, registry: MetricsRegistry
    ) -> None:
        registry.for_tenant("acme").increment("test_things_total", kind="a")
        assert registry.series("test_things_total") == ((("tenant", "acme"), ("kind", "a")),)

    def test_a_spec_may_not_declare_a_tenant_label(self) -> None:
        with pytest.raises(ValueError, match="do not declare a 'tenant' label"):
            InstrumentSpec(
                name="test_bad_total",
                kind=InstrumentKind.COUNTER,
                description="d",
                decision="d",
                labels=("tenant",),
            )

    def test_a_process_wide_instrument_needs_no_tenant(self, registry: MetricsRegistry) -> None:
        registry.increment("test_process_events_total", kind="startup")
        assert registry.value("test_process_events_total", kind="startup") == 1
        assert registry.series("test_process_events_total") == ((("kind", "startup"),),)

    def test_a_tenant_handle_can_still_write_process_wide_instruments(
        self, registry: MetricsRegistry
    ) -> None:
        registry.for_tenant("acme").increment("test_process_events_total", kind="startup")
        assert registry.value("test_process_events_total", kind="startup") == 1

    def test_reading_a_value_requires_the_full_label_set(self, registry: MetricsRegistry) -> None:
        """There is no "across tenants" read. That is what Prometheus is for, and offering
        it here would be a route to a series the caller was never handed a tenant for."""
        registry.for_tenant("acme").increment("test_things_total", kind="a")
        assert registry.value("test_things_total", kind="a") is None
        assert registry.value("test_things_total", tenant="acme", kind="a") == 1


# ----------------------------------------------------------------- refusals


class TestRefusals:
    """Every refusal is counted, so the catalogue explains its own silences."""

    @pytest.mark.parametrize(
        ("call", "reason"),
        [
            (lambda m: m.increment("test_nonexistent_total"), DropReason.UNREGISTERED),
            (lambda m: m.increment("test_duration_seconds"), DropReason.WRONG_KIND),
            (lambda m: m.observe("test_things_total", 1.0), DropReason.WRONG_KIND),
            (lambda m: m.increment("test_things_total", -1, kind="a"), DropReason.BAD_VALUE),
            (lambda m: m.observe("test_duration_seconds", math.nan), DropReason.BAD_VALUE),
            (lambda m: m.increment("test_things_total", nope="x"), DropReason.UNKNOWN_LABEL),
        ],
    )
    def test_a_refused_recording_is_counted(
        self, registry: MetricsRegistry, call: object, reason: DropReason
    ) -> None:
        metrics = registry.for_tenant("acme")
        call(metrics)  # type: ignore[operator]
        total = sum(
            registry.value("observability_dropped_total", instrument=name, reason=reason.value) or 0
            for name in (
                "test_nonexistent_total",
                "test_duration_seconds",
                "test_things_total",
            )
        )
        assert total == 1

    def test_an_omitted_label_becomes_empty_rather_than_a_refusal(
        self, registry: MetricsRegistry
    ) -> None:
        """Every series of one instrument must have the same label set; an empty string is
        what a Prometheus client fills with, and dropping the sample would be worse."""
        registry.for_tenant("acme").increment("test_things_total")
        assert registry.value("test_things_total", tenant="acme", kind="") == 1

    def test_cardinality_is_capped(self, registry: MetricsRegistry) -> None:
        metrics = registry.for_tenant("acme")
        for index in range(MAX_SERIES_PER_INSTRUMENT + 25):
            metrics.increment("test_things_total", kind=f"k{index}")

        assert len(registry.series("test_things_total")) == MAX_SERIES_PER_INSTRUMENT
        assert (
            registry.value(
                "observability_dropped_total",
                instrument="test_things_total",
                reason=DropReason.CARDINALITY.value,
            )
            == 25
        )

    def test_a_label_value_is_scrubbed_like_a_log_field(self, registry: MetricsRegistry) -> None:
        """A label is exported exactly as far as a log line is. A token in one is the same
        disclosure as a token in the other."""
        registry.for_tenant("acme").increment(
            "test_things_total", kind="Bearer eyJhbGciOiJIUzI1NiJ9.abcdef"
        )
        rendered = registry.render()
        assert "eyJhbGciOiJIUzI1NiJ9" not in rendered
        assert "[redacted]" in rendered

    def test_a_label_value_never_contains_a_newline(self, registry: MetricsRegistry) -> None:
        registry.for_tenant("acme").increment("test_things_total", kind="a\nb")
        line = next(
            line for line in registry.render().splitlines() if line.startswith("test_things_total{")
        )
        assert 'kind="a b"' in line


class TestRegistration:
    """Registration is the one place this module raises, and it happens at import."""

    def test_registering_the_same_spec_twice_is_a_no_op(self, registry: MetricsRegistry) -> None:
        registry.register(COUNTER)
        registry.register(COUNTER)
        assert len([spec for spec in registry.specs() if spec.name == COUNTER.name]) == 1

    def test_a_conflicting_redefinition_is_refused(self, registry: MetricsRegistry) -> None:
        with pytest.raises(SpecConflictError):
            registry.register(
                InstrumentSpec(
                    name="test_things_total",
                    kind=InstrumentKind.COUNTER,
                    description="Something else entirely.",
                    decision="A different decision.",
                    labels=("other",),
                )
            )

    def test_a_foreign_catalogue_registers_without_touching_this_package(self) -> None:
        """The extension point the voice and protocol layers need (spec 19.13).

        They define their own specs beside their own code and register them; nothing in
        ``platform_observability`` is edited, and both catalogues render from one registry.
        """
        registry = MetricsRegistry()
        voice = (
            InstrumentSpec(
                name="voice_frames_total",
                kind=InstrumentKind.COUNTER,
                description="Audio frames handled, by direction and disposition.",
                decision="Whether the queue is evicting under load.",
                labels=("direction", "disposition"),
            ),
            InstrumentSpec(
                name="voice_queue_depth",
                kind=InstrumentKind.GAUGE,
                description="Frames waiting to be sent to the transcriber.",
                decision="Whether to widen the queue or drop earlier.",
            ),
        )
        registry.register_all(voice)
        registry.for_tenant("acme").increment(
            "voice_frames_total", direction="inbound", disposition="sent"
        )

        text = registry.render()
        assert "# TYPE voice_frames_total counter" in text
        assert "# TYPE voice_queue_depth gauge" in text
        assert 'voice_frames_total{tenant="acme",direction="inbound",disposition="sent"} 1' in text

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"name": "0bad"}, "not a valid Prometheus metric name"),
            ({"description": "  "}, "description is required"),
            ({"decision": ""}, "'decision' is required"),
            ({"labels": ("a", "a")}, "duplicate label"),
            ({"labels": ("a-b",)}, "not a valid label name"),
            ({"name": "test_things"}, "ends in '_total'"),
        ],
    )
    def test_a_malformed_spec_is_refused_at_definition(
        self, kwargs: dict[str, object], match: str
    ) -> None:
        base: dict[str, object] = {
            "name": "test_things_total",
            "kind": InstrumentKind.COUNTER,
            "description": "d",
            "decision": "d",
        }
        with pytest.raises(ValueError, match=match):
            InstrumentSpec(**{**base, **kwargs})  # type: ignore[arg-type]

    def test_a_histogram_needs_buckets_and_a_counter_may_not_have_them(self) -> None:
        with pytest.raises(ValueError, match="needs explicit buckets"):
            InstrumentSpec(
                name="test_no_buckets_seconds",
                kind=InstrumentKind.HISTOGRAM,
                description="d",
                decision="d",
            )
        with pytest.raises(ValueError, match="only a histogram has buckets"):
            InstrumentSpec(
                name="test_bucketed_total",
                kind=InstrumentKind.COUNTER,
                description="d",
                decision="d",
                buckets=(1.0,),
            )


# -------------------------------------------------------------------- sinks


@dataclass
class _Spy:
    """A sink that records what it was handed."""

    seen: list[Sample]

    def observe(self, sample: Sample) -> None:
        self.seen.append(sample)


class _Poisoned:
    """A sink that is down. The metrics backend, on a bad day."""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, sample: Sample) -> None:
        self.calls += 1
        raise ConnectionError("the metrics backend is unreachable")


class TestSinks:
    """A failing sink is the whole reason this package has no network client."""

    def test_a_sink_receives_every_sample(self, registry: MetricsRegistry) -> None:
        spy = _Spy(seen=[])
        registry.add_sink(spy, name="spy")
        registry.for_tenant("acme").increment("test_things_total", kind="a")

        assert [sample.name for sample in spy.seen] == ["test_things_total"]
        assert spy.seen[0].labels == (("tenant", "acme"), ("kind", "a"))
        assert spy.seen[0].value == 1.0

    def test_a_failing_sink_never_raises_into_its_caller(self, registry: MetricsRegistry) -> None:
        """The property the platform depends on: if the metrics backend is down, commerce
        continues. Not caught by the caller -- never raised in the first place."""
        registry.add_sink(_Poisoned(), name="poisoned")
        metrics = registry.for_tenant("acme")

        metrics.increment("test_things_total", kind="a")  # must not raise
        metrics.observe("test_duration_seconds", 0.1, operation="submit")
        metrics.set_gauge("test_depth", 4, queue="outbox")

        assert registry.value("test_things_total", tenant="acme", kind="a") == 1, (
            "the value is still recorded locally; only the export failed"
        )
        assert registry.value("observability_sink_failures_total", sink="poisoned") == 3

    def test_a_persistently_failing_sink_is_quarantined(self, registry: MetricsRegistry) -> None:
        poisoned = _Poisoned()
        registry.add_sink(poisoned, name="poisoned")
        metrics = registry.for_tenant("acme")

        for _ in range(SINK_FAILURE_LIMIT + 10):
            metrics.increment("test_things_total", kind="a")

        assert registry.quarantined_sinks() == ("poisoned",)
        assert poisoned.calls == SINK_FAILURE_LIMIT, (
            "a quarantined sink stops being called at all, rather than being retried "
            "on every recording for the life of the process"
        )

    def test_one_failing_sink_does_not_starve_another(self, registry: MetricsRegistry) -> None:
        spy = _Spy(seen=[])
        registry.add_sink(_Poisoned(), name="poisoned")
        registry.add_sink(spy, name="spy")
        registry.for_tenant("acme").increment("test_things_total", kind="a")
        assert len(spy.seen) == 1

    def test_recording_returns_none(self, registry: MetricsRegistry) -> None:
        """No caller can branch on whether a metric was recorded, which is what makes it
        impossible for a money decision to come to depend on one."""
        metrics = registry.for_tenant("acme")
        assert metrics.increment("test_things_total", kind="a") is None
        assert metrics.set_gauge("test_depth", 1, queue="q") is None
        assert metrics.add_gauge("test_depth", 1, queue="q") is None
        assert metrics.observe("test_duration_seconds", 0.1, operation="submit") is None
        assert registry.increment("test_process_events_total", kind="k") is None


class TestGauges:
    def test_set_replaces_and_add_accumulates(self, registry: MetricsRegistry) -> None:
        metrics = registry.for_tenant("acme")
        metrics.set_gauge("test_depth", 10, queue="outbox")
        metrics.add_gauge("test_depth", -3, queue="outbox")
        assert registry.value("test_depth", tenant="acme", queue="outbox") == 7
        metrics.set_gauge("test_depth", 2, queue="outbox")
        assert registry.value("test_depth", tenant="acme", queue="outbox") == 2

    def test_a_gauge_may_go_negative_and_a_counter_may_not(self, registry: MetricsRegistry) -> None:
        metrics = registry.for_tenant("acme")
        metrics.add_gauge("test_depth", -5, queue="outbox")
        assert registry.value("test_depth", tenant="acme", queue="outbox") == -5

        metrics.increment("test_things_total", -5, kind="a")
        assert registry.value("test_things_total", tenant="acme", kind="a") is None
