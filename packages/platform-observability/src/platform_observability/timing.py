"""Duration and outcome for a span of work, without an OpenTelemetry dependency.

Specification 22 names OpenTelemetry, Cloud Trace and Cloud Monitoring as the eventual
telemetry stack. This package does not import any of them, for the reason in
:mod:`platform_observability.metrics`: an SDK brings an exporter, an exporter brings a
socket and a queue, and a socket on the path of a payment is exactly the coupling this
layer exists to avoid. What a tracer would give that a histogram does not -- the causal
tree across processes -- is real, so the shape is here as a seam and the dependency is not.

The seam is :class:`Tracer`, two methods wide. Handing a real implementation to
:func:`set_tracer` (or to a single call site) makes every :func:`timed` block start and end
a span; the default does nothing at all. Every call into a tracer is wrapped, so a tracer
that raises or blocks is a tracer that gets quarantined, not an outage.

Correlation is passed through as a span attribute. It is not emitted as a Prometheus
*exemplar*, which is the other way a trace id joins to a metric: exemplars are an
OpenMetrics feature and this module renders the 0.0.4 text format, where there is nowhere
to put one. The join is therefore made in the log store and in the tracer, both of which
carry the same ``correlation_id`` the audit rows carry.

Usage::

    with timed(metrics, PROVIDER_CALL, provider="razorpay", operation="orders.create") as span:
        response = client.post(...)
        span.set_outcome(_classify(response))

A plain ``with`` inside ``async def`` is correct and deliberate: the block measures wall
time across the awaits it contains, and a synchronous context manager needs no ``async
with`` to do that. It also means one implementation serves the API, the Action Executor and the
voice loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from .correlation import correlation_id
from .metrics import MetricsRegistry, TenantMetrics

__all__ = [
    "Outcome",
    "Span",
    "SpanHandle",
    "Timing",
    "Tracer",
    "set_tracer",
    "timed",
    "tracer",
]

_LOG: Final = logging.getLogger("platform_observability.timing")


class Outcome(StrEnum):
    """The outcome label every timed span carries.

    Deliberately coarse. A span's outcome answers "did this work", and the *reason* it did
    not belongs on a denial counter with a reason code -- ``RecoveryCode`` for the kernel,
    an HTTP status for the provider -- where the cardinality is understood. A block that
    wants a finer verdict calls :meth:`Span.set_outcome` with its own string.
    """

    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Timing:
    """The instruments a timed block writes: a duration histogram, optionally a counter.

    Named once, as data, next to the instrument definitions rather than at each call site,
    so the same block cannot be timed into two differently-labelled histograms by two
    callers::

        PROVIDER_CALL = Timing(
            duration="commerce_provider_request_duration_seconds",
            outcome_counter="commerce_provider_requests_total",
        )

    **When there is an outcome counter, the outcome label goes on the histogram too**, and
    both instruments must declare it. That is one rule rather than two, and it is the
    useful one: failures are usually fast, so a latency histogram that mixes them in
    flatters its own percentiles and hides the slow successes that are the actual problem.
    A ``Timing`` with no outcome counter puts no outcome label anywhere.
    """

    duration: str
    outcome_counter: str | None = None
    outcome_label: str = "outcome"


class SpanHandle(Protocol):
    """A started span, from the tracer's point of view."""

    def end(self, *, outcome: str, error_type: str | None) -> None: ...


class Tracer(Protocol):
    """The OpenTelemetry-shaped seam. Two methods, no import.

    An adapter over ``opentelemetry.trace`` is a few lines and lives in whatever package
    owns that dependency -- not in this one, which stays free of it.
    """

    def start_span(self, name: str, attributes: Mapping[str, str]) -> SpanHandle: ...


class _NullSpan:
    """The default span: ending it does nothing."""

    def end(self, *, outcome: str, error_type: str | None) -> None:
        """Discard the span. Arguments are accepted and ignored: this is the shape a real
        tracer's handle has, and the no-op has to match it exactly or installing one would
        change the call signature at every site."""


class _NullTracer:
    """The tracer installed until :func:`set_tracer` is given a real one.

    Its existence is why a call site can use :func:`timed` before anybody has decided
    about tracing: the seam is always there and costs one attribute lookup.
    """

    __slots__ = ("_span",)

    def __init__(self) -> None:
        self._span = _NullSpan()

    def start_span(self, name: str, attributes: Mapping[str, str]) -> SpanHandle:  # noqa: ARG002
        return self._span


_TRACER: Tracer = _NullTracer()


def set_tracer(new_tracer: Tracer | None) -> None:
    """Install a tracer process-wide, or ``None`` to remove one.

    Process-wide rather than per-registry because a trace is a property of the process's
    relationship to a collector, not of any one metrics catalogue.
    """
    global _TRACER
    _TRACER = _NullTracer() if new_tracer is None else new_tracer


def tracer() -> Tracer:
    """The installed tracer, or the no-op one."""
    return _TRACER


@dataclass(slots=True)
class Span:
    """The live handle inside a :func:`timed` block.

    :meth:`set_outcome` overrides the outcome the block would otherwise get, for the case
    an operation "succeeded" in the sense of not raising and failed in the sense that
    matters -- a provider 502, a kernel denial, a timeout classified as ``PAYMENT_UNKNOWN``.

    :meth:`label` adds a label value discovered part-way through, which is the normal case
    for an HTTP status or a resolved route template.
    """

    name: str
    labels: dict[str, str]
    outcome: str = Outcome.OK.value
    started: float = 0.0

    def set_outcome(self, outcome: str) -> None:
        self.outcome = str(outcome)

    def label(self, name: str, value: object) -> None:
        self.labels[name] = str(value)

    @property
    def elapsed(self) -> float:
        """Seconds since the block began. Monotonic, so a clock adjustment cannot make it
        negative -- and a negative latency is the one value that poisons a histogram
        permanently."""
        return time.perf_counter() - self.started


@contextmanager
def timed(
    metrics: MetricsRegistry | TenantMetrics,
    timing: Timing,
    **labels: object,
) -> Iterator[Span]:
    """Time a block; record its duration and outcome; re-raise whatever it raised.

    The exception propagates: it is the caller's control flow and this package has no
    business changing it. What does *not* propagate is any failure of the recording itself
    -- the histogram, the counter and the tracer are each independently guarded, so a
    broken observability layer cannot turn a successful payment into a failed one.

    On an exception the outcome becomes ``error`` and an ``error_type`` label carrying the
    exception's class name is offered to the tracer. It is not added to the metric labels,
    because an exception type is unbounded cardinality and the counter's ``outcome`` label
    already says what a dashboard needs.
    """
    span = Span(name=timing.duration, labels={k: str(v) for k, v in labels.items()})
    span.started = time.perf_counter()
    handle = _start(timing.duration, span)
    error_type: str | None = None
    try:
        yield span
    except BaseException as exc:
        # BaseException, not Exception: a cancelled task or a KeyboardInterrupt mid-call is
        # exactly the case where knowing the span did not finish matters most.
        span.outcome = Outcome.ERROR.value
        error_type = type(exc).__name__
        raise
    finally:
        elapsed = span.elapsed
        _observe(metrics, timing, span, elapsed)
        _end(handle, span, error_type)


def _start(name: str, span: Span) -> SpanHandle | None:
    attributes = dict(span.labels)
    current = correlation_id()
    if current is not None:
        attributes["correlation_id"] = current
    try:
        return _TRACER.start_span(name, attributes)
    except Exception as exc:
        _tracer_failed("start_span", exc)
        return None


def _end(handle: SpanHandle | None, span: Span, error_type: str | None) -> None:
    if handle is None:
        return
    try:
        handle.end(outcome=span.outcome, error_type=error_type)
    except Exception as exc:
        _tracer_failed("end", exc)


def _tracer_failed(operation: str, exc: BaseException) -> None:
    _LOG.warning("tracer %s failed: %s", operation, type(exc).__name__)


def _observe(
    metrics: MetricsRegistry | TenantMetrics, timing: Timing, span: Span, elapsed: float
) -> None:
    """Write the histogram, then the outcome counter. Both recordings are already total.

    The outcome is merged into a copy of the labels rather than splatted alongside them: a
    block that called ``span.label("outcome", ...)`` would otherwise produce a duplicate
    keyword argument, and a ``TypeError`` raised out of a ``finally`` would take the
    caller's own exception with it.
    """
    labels = dict(span.labels)
    if timing.outcome_counter is not None:
        labels[timing.outcome_label] = span.outcome
    metrics.observe(timing.duration, elapsed, **labels)
    if timing.outcome_counter is not None:
        metrics.increment(timing.outcome_counter, **labels)
