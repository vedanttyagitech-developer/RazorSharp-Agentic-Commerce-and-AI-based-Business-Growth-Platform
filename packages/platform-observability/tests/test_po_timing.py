"""A timed block records duration and outcome, and changes nothing else about the block.

The two properties that matter: the caller's exception reaches the caller unchanged, and
no failure of the recording -- histogram, counter or tracer -- reaches them at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping

import pytest
from platform_observability import (
    InstrumentKind,
    InstrumentSpec,
    MetricsRegistry,
    Outcome,
    SpanHandle,
    Timing,
    bind_scope,
    set_tracer,
    timed,
)
from platform_observability.metrics import LATENCY_BUCKETS

DURATION = InstrumentSpec(
    name="test_call_duration_seconds",
    kind=InstrumentKind.HISTOGRAM,
    description="How long a call took.",
    decision="Where the timeout budget should sit.",
    labels=("operation", "outcome"),
    unit="seconds",
    buckets=LATENCY_BUCKETS,
)

CALLS = InstrumentSpec(
    name="test_calls_total",
    kind=InstrumentKind.COUNTER,
    description="Calls made.",
    decision="Whether the provider is failing or we are.",
    labels=("operation", "outcome"),
)

CALL = Timing(duration=DURATION.name, outcome_counter=CALLS.name)


@pytest.fixture
def registry() -> MetricsRegistry:
    reg = MetricsRegistry()
    reg.register_all((DURATION, CALLS))
    return reg


@pytest.fixture(autouse=True)
def _no_tracer() -> Iterator[None]:
    """Leave the process-wide seam as it was found, whatever a test installs."""
    yield
    set_tracer(None)


class _RecordingSpan:
    def __init__(self, log: list[tuple[str, str, str | None]], name: str) -> None:
        self._log = log
        self._name = name

    def end(self, *, outcome: str, error_type: str | None) -> None:
        self._log.append((self._name, outcome, error_type))


class _RecordingTracer:
    def __init__(self) -> None:
        self.ended: list[tuple[str, str, str | None]] = []
        self.attributes: list[Mapping[str, str]] = []

    def start_span(self, name: str, attributes: Mapping[str, str]) -> SpanHandle:
        self.attributes.append(dict(attributes))
        return _RecordingSpan(self.ended, name)


class _HostileTracer:
    """A collector that is down, in the two places it can be down."""

    def start_span(self, name: str, attributes: Mapping[str, str]) -> SpanHandle:
        raise ConnectionError("the collector is unreachable")


class TestRecording:
    def test_a_successful_block_records_a_duration_and_an_ok_outcome(
        self, registry: MetricsRegistry
    ) -> None:
        metrics = registry.for_tenant("acme")
        with timed(metrics, CALL, operation="orders.create"):
            pass

        snapshot = registry.histogram(
            DURATION.name, tenant="acme", operation="orders.create", outcome=Outcome.OK.value
        )
        assert snapshot is not None
        assert snapshot.count == 1
        assert (
            registry.value(
                CALLS.name, tenant="acme", operation="orders.create", outcome=Outcome.OK.value
            )
            == 1
        )

    def test_the_outcome_can_be_overridden_inside_the_block(
        self, registry: MetricsRegistry
    ) -> None:
        """An operation that did not raise and still failed -- a provider 502, a payment
        classified as unknown -- is the normal case, not the exception."""
        with timed(registry.for_tenant("acme"), CALL, operation="orders.fetch") as span:
            span.set_outcome("server_error")

        assert (
            registry.value(
                CALLS.name, tenant="acme", operation="orders.fetch", outcome="server_error"
            )
            == 1
        )

    def test_a_label_can_be_added_part_way_through(self, registry: MetricsRegistry) -> None:
        with timed(registry.for_tenant("acme"), CALL) as span:
            span.label("operation", "orders.create")

        assert registry.series(CALLS.name) == (
            (("tenant", "acme"), ("operation", "orders.create"), ("outcome", "ok")),
        )

    def test_a_block_can_be_timed_through_the_bare_registry(
        self, registry: MetricsRegistry
    ) -> None:
        """A tenant-scoped instrument through the bare registry is dropped, not recorded --
        and still does not raise, which is what a timed block has to guarantee."""
        with timed(registry, CALL, operation="orders.create"):
            pass
        assert registry.series(CALLS.name) == ()


class TestExceptions:
    def test_the_caller_s_exception_propagates_unchanged(self, registry: MetricsRegistry) -> None:
        with (
            pytest.raises(ValueError, match="boom"),
            timed(registry.for_tenant("acme"), CALL, operation="orders.create"),
        ):
            raise ValueError("boom")

    def test_a_raised_block_is_recorded_as_an_error(self, registry: MetricsRegistry) -> None:
        with (
            pytest.raises(ValueError),
            timed(registry.for_tenant("acme"), CALL, operation="orders.create"),
        ):
            raise ValueError("boom")

        assert (
            registry.value(
                CALLS.name, tenant="acme", operation="orders.create", outcome=Outcome.ERROR.value
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_task_is_recorded_as_an_error(
        self, registry: MetricsRegistry
    ) -> None:
        """``BaseException``, not ``Exception``: a cancellation mid-provider-call is the
        case where knowing the span did not finish matters most."""

        async def work() -> None:
            with timed(registry.for_tenant("acme"), CALL, operation="orders.create"):
                await asyncio.sleep(10)

        task = asyncio.create_task(work())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert (
            registry.value(
                CALLS.name, tenant="acme", operation="orders.create", outcome=Outcome.ERROR.value
            )
            == 1
        )

    def test_a_duplicate_outcome_label_does_not_raise_out_of_the_finally(
        self, registry: MetricsRegistry
    ) -> None:
        """A ``TypeError`` from a duplicated keyword would escape the ``finally`` and take
        the caller's own exception with it -- the observability layer replacing the
        diagnosis of a real failure with a diagnosis of itself."""
        with timed(registry.for_tenant("acme"), CALL, operation="orders.create") as span:
            span.label("outcome", "supplied-by-the-caller")

        assert (
            registry.value(
                CALLS.name, tenant="acme", operation="orders.create", outcome=Outcome.OK.value
            )
            == 1
        )


class TestTheTracerSeam:
    def test_no_tracer_is_installed_by_default(self, registry: MetricsRegistry) -> None:
        with timed(registry.for_tenant("acme"), CALL, operation="orders.create"):
            pass  # nothing to assert but that it did not blow up

    def test_an_installed_tracer_sees_the_span_and_its_correlation_id(
        self, registry: MetricsRegistry
    ) -> None:
        recorder = _RecordingTracer()
        set_tracer(recorder)

        with (
            bind_scope("corr-1"),
            timed(registry.for_tenant("acme"), CALL, operation="orders.create") as span,
        ):
            span.set_outcome("timeout")

        assert recorder.ended == [(DURATION.name, "timeout", None)]
        assert recorder.attributes[0]["correlation_id"] == "corr-1"
        assert recorder.attributes[0]["operation"] == "orders.create"

    def test_the_tracer_is_told_the_exception_type(self, registry: MetricsRegistry) -> None:
        recorder = _RecordingTracer()
        set_tracer(recorder)

        with (
            pytest.raises(TimeoutError),
            timed(registry.for_tenant("acme"), CALL, operation="orders.create"),
        ):
            raise TimeoutError

        assert recorder.ended == [(DURATION.name, Outcome.ERROR.value, "TimeoutError")]

    def test_a_tracer_that_raises_never_reaches_the_caller(self, registry: MetricsRegistry) -> None:
        """The same property the metrics sink has, for the same reason: a collector being
        down must not be able to fail a payment."""
        set_tracer(_HostileTracer())

        with timed(registry.for_tenant("acme"), CALL, operation="orders.create"):
            pass

        assert (
            registry.value(
                CALLS.name, tenant="acme", operation="orders.create", outcome=Outcome.OK.value
            )
            == 1
        ), "the metric is still recorded when the tracer is not"


class TestElapsed:
    def test_duration_is_measured_on_a_monotonic_clock(self, registry: MetricsRegistry) -> None:
        """A wall clock adjusted backwards mid-call would otherwise produce a negative
        latency, which poisons a histogram permanently."""
        with timed(registry.for_tenant("acme"), CALL, operation="orders.create") as span:
            first = span.elapsed
            second = span.elapsed
        assert 0 <= first <= second

    @pytest.mark.asyncio
    async def test_a_timed_block_measures_across_awaits(self, registry: MetricsRegistry) -> None:
        """A synchronous context manager is the right shape for an async block: it measures
        wall time across the awaits it contains, and one implementation serves the API, the
        worker and the voice loop."""
        with timed(registry.for_tenant("acme"), CALL, operation="orders.create"):
            await asyncio.sleep(0.01)

        snapshot = registry.histogram(
            DURATION.name, tenant="acme", operation="orders.create", outcome=Outcome.OK.value
        )
        assert snapshot is not None
        assert snapshot.total >= 0.01
