"""A registry and a renderer. No client, no push, no thread, no opinion about mounting.

What this is not
----------------
It is not the audit stream. ``transaction_kernel.audit`` writes gapless, hash-chained,
tenant-scoped rows in the same transaction as the state change they record, and losing one
is a corruption incident. A counter here is the opposite of that in every respect: it is
in memory, it is aggregated, it is reset by a restart, and losing one must be a
non-event. **A counter is never the record of a money action.** ``commerce_refunds_open``
is a gauge saying how many refunds are waiting; the refund itself is the row in ``refunds``
and the chain in ``audit_events``, and nothing may ever be reconstructed from the metric.
Read the two names next to each other: one answers "is the platform healthy", the other
answers "what happened to this buyer's money", and a system that confuses them ends up
reconciling a ledger against a Prometheus scrape.

Why there is no network client in here
--------------------------------------
If the metrics backend is down, commerce must continue unaffected, and that should be
structurally true rather than merely intended. A registry that pushes has a socket, a
timeout, a retry policy and a queue, and every one of those can block a request thread
while a payment is in flight. A registry that only *renders* has none of them. Whoever
mounts this owns the endpoint, and a Prometheus scrape that fails is Prometheus's problem
rather than the checkout's.

The same reasoning produces the guards. Every public recording method is total: it catches
its own exceptions, counts them on :data:`SELF_INSTRUMENTS`, and returns ``None``. No
recording call in this module returns anything a caller could branch on, so no code path
can be made to depend on the metric having worked.

Tenancy
-------
Every platform instrument is tenant-labelled, and the tenant label is *not* something a
caller passes. It is injected by :class:`TenantMetrics`, obtained from
:meth:`MetricsRegistry.for_tenant`. A component holding tenant A's handle has no argument
it can supply that reaches tenant B's series -- an explicit ``tenant`` label is refused and
counted rather than honoured. That is the structural form of the isolation the rest of the
platform gets from row-level security.

Cardinality
-----------
An unbounded label is how a metrics system takes down the process it was meant to observe.
Series per instrument are capped at :data:`MAX_SERIES_PER_INSTRUMENT`; past the cap, new
label combinations are dropped and counted. Dropping is the right failure direction here
for exactly the reason the audit stream's is the opposite: nobody reconstructs anything
from these numbers.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from .correlation import current_tenant
from .redaction import scrub_text

__all__ = [
    "AGE_BUCKETS",
    "LATENCY_BUCKETS",
    "MAX_LABEL_VALUE_LENGTH",
    "MAX_SERIES_PER_INSTRUMENT",
    "PROMETHEUS_CONTENT_TYPE",
    "SELF_INSTRUMENTS",
    "SINK_FAILURE_LIMIT",
    "TENANT_LABEL",
    "DropReason",
    "HistogramSnapshot",
    "InstrumentKind",
    "InstrumentSpec",
    "MetricsRegistry",
    "MetricsSink",
    "Sample",
    "SpecConflictError",
    "TenantMetrics",
]

_LOG: Final = logging.getLogger("platform_observability.metrics")

#: What a Prometheus scrape must be served as. This module renders the 0.0.4 text format;
#: the version parameter is not decorative, and a scraper handed a bare ``text/plain``
#: will guess.
PROMETHEUS_CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"

#: Seconds. Fine at the bottom because an in-process kernel admission should be single-digit
#: milliseconds, and long at the top because a Razorpay call over a bad link is the latency
#: this platform actually has to be able to see.
LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

#: Seconds, for how long something has been waiting: a pending outbox row, a webhook not
#: yet applied, a refund in an unknown state. A minute to a day, because the interesting
#: question about a stuck work item is which of those it has been stuck for.
AGE_BUCKETS: Final[tuple[float, ...]] = (
    1.0,
    5.0,
    15.0,
    60.0,
    300.0,
    900.0,
    3600.0,
    21600.0,
    86400.0,
)

#: Past this many distinct label combinations, an instrument stops accepting new ones.
MAX_SERIES_PER_INSTRUMENT: Final[int] = 2000

#: A label value longer than this is cut. Prometheus imposes no limit; storage engines do,
#: and a label is meant to be an enum member rather than a sentence.
MAX_LABEL_VALUE_LENGTH: Final[int] = 120

#: The label this module owns. A spec may not declare it and a caller may not pass it;
#: :class:`TenantMetrics` is the only thing that sets it.
TENANT_LABEL: Final[str] = "tenant"

#: A sink failing this many times in a row is quarantined: dropped from the dispatch list
#: with one log line. Consecutive rather than cumulative, so a backend that recovers is not
#: punished for last Tuesday's outage.
SINK_FAILURE_LIMIT: Final[int] = 5

_METRIC_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class InstrumentKind(StrEnum):
    """The three shapes Prometheus understands, and the only three offered.

    Summaries are omitted on purpose: their quantiles cannot be aggregated across
    processes, and every deployment of this platform is more than one process.
    """

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class DropReason(StrEnum):
    """Why a recording was refused. Every value appears on ``observability_dropped_total``."""

    UNREGISTERED = "unregistered"
    """No spec by that name. Almost always a typo at a call site."""

    WRONG_KIND = "wrong_kind"
    """``increment`` on a histogram, ``observe`` on a counter."""

    MISSING_TENANT = "missing_tenant"
    """A tenant-scoped instrument written through the bare registry, or through a handle
    from :meth:`MetricsRegistry.for_current_scope` with no tenant bound."""

    TENANT_OVERRIDE = "tenant_override"
    """A caller holding one tenant's handle tried to set the tenant label itself."""

    UNKNOWN_LABEL = "unknown_label"
    """A label name the spec does not declare."""

    CARDINALITY = "cardinality"
    """:data:`MAX_SERIES_PER_INSTRUMENT` reached for this instrument."""

    BAD_VALUE = "bad_value"
    """NaN, an infinity, or a negative counter increment."""


class SpecConflictError(ValueError):
    """Two different specs claim one instrument name.

    The only exception this module raises, and it can only happen at registration -- which
    happens at import or at mount, never on a request. Registering the *same* spec twice
    does nothing, so two packages importing one shared catalogue do not collide.
    Registering a *different* one is a programming error that would make the exposition
    self-contradictory, and it should stop the process at startup rather than produce a
    metric nobody can interpret.
    """


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """One instrument, as data.

    ``decision`` is required, and it is the field that makes this a catalogue rather than a
    list of names. Specification 19.13: "Counters that are never surfaced are not
    observability." A counter nobody can name a decision for is the same failure one step
    earlier -- it gets surfaced on a dashboard nobody reads, because nobody knows what
    seeing it should make them do. Writing the sentence at definition time is cheap, and it
    is the only moment anyone will be in a position to write it honestly.

    ``labels`` must **not** include ``tenant``: it is injected for every ``tenant_scoped``
    instrument and refused as a declared label, so there is exactly one way for a tenant to
    end up on a series.
    """

    name: str
    kind: InstrumentKind
    description: str
    decision: str
    labels: tuple[str, ...] = ()
    unit: str | None = None
    buckets: tuple[float, ...] | None = None
    tenant_scoped: bool = True

    def __post_init__(self) -> None:
        if not _METRIC_NAME.match(self.name):
            raise ValueError(f"{self.name!r} is not a valid Prometheus metric name")
        if self.name.startswith("__"):
            raise ValueError(f"{self.name!r} uses the reserved '__' prefix")
        if not self.description.strip():
            raise ValueError(f"{self.name}: description is required; it becomes # HELP")
        if not self.decision.strip():
            raise ValueError(
                f"{self.name}: 'decision' is required -- name the decision this instrument "
                "informs, or do not add the instrument (specification 19.13)"
            )
        self._validate_labels()
        self._validate_kind()

    def _validate_labels(self) -> None:
        seen: set[str] = set()
        for label in self.labels:
            if not _LABEL_NAME.match(label) or label.startswith("__"):
                raise ValueError(f"{self.name}: {label!r} is not a valid label name")
            if label == TENANT_LABEL:
                raise ValueError(
                    f"{self.name}: do not declare a 'tenant' label; set tenant_scoped and "
                    "obtain a handle from MetricsRegistry.for_tenant"
                )
            if label in seen:
                raise ValueError(f"{self.name}: duplicate label {label!r}")
            seen.add(label)

    def _validate_kind(self) -> None:
        if self.kind is InstrumentKind.COUNTER and not self.name.endswith("_total"):
            raise ValueError(f"{self.name}: a counter's name ends in '_total' by convention")
        if self.kind is not InstrumentKind.COUNTER and self.name.endswith("_total"):
            raise ValueError(f"{self.name}: '_total' means counter; this is a {self.kind}")
        if self.kind is InstrumentKind.HISTOGRAM:
            if not self.buckets:
                raise ValueError(f"{self.name}: a histogram needs explicit buckets")
            if list(self.buckets) != sorted(set(self.buckets)):
                raise ValueError(f"{self.name}: buckets must be sorted and distinct")
            if not all(math.isfinite(bound) for bound in self.buckets):
                raise ValueError(f"{self.name}: bucket bounds must be finite (+Inf is implicit)")
        elif self.buckets is not None:
            raise ValueError(f"{self.name}: only a histogram has buckets")
        if self.unit == "seconds" and not self.name.endswith("_seconds"):
            raise ValueError(f"{self.name}: a seconds instrument is named '..._seconds'")

    @property
    def label_names(self) -> tuple[str, ...]:
        """The full ordered label set, tenant first when the instrument is tenant-scoped."""
        return (TENANT_LABEL, *self.labels) if self.tenant_scoped else self.labels


@dataclass(frozen=True, slots=True)
class Sample:
    """One value handed to a :class:`MetricsSink`. The seam, and nothing more."""

    name: str
    kind: InstrumentKind
    labels: tuple[tuple[str, str], ...]
    value: float


@runtime_checkable
class MetricsSink(Protocol):
    """Somewhere else a sample can go: an OpenTelemetry meter, a StatsD line, a test spy.

    A sink is called synchronously on the recording path, which is why every call is
    wrapped and why one that keeps failing is dropped. **A sink must not block.** One that
    does is a network client wearing a protocol's clothes, and the reasoning in the module
    docstring applies to it in full.
    """

    def observe(self, sample: Sample) -> None: ...


@dataclass(frozen=True, slots=True)
class HistogramSnapshot:
    """One histogram series, read back. For assertions, and for rendering."""

    buckets: tuple[tuple[float, int], ...]
    """Cumulative ``(upper_bound, count)`` pairs, excluding the implicit ``+Inf``."""

    count: int
    total: float


@dataclass(slots=True)
class _HistogramState:
    """Per-bucket counts held non-cumulatively; cumulated when read."""

    counts: list[int]
    total: float = 0.0
    observations: int = 0


@dataclass(slots=True)
class _SinkState:
    sink: MetricsSink
    name: str
    consecutive_failures: int = 0
    quarantined: bool = False


# --------------------------------------------------------------- the registry's own


#: What the registry says about itself. Registered into every :class:`MetricsRegistry` at
#: construction, so a registry can always account for what it refused. Not tenant-scoped: a
#: drop is a fact about this process, and attributing one to a tenant would need the very
#: tenant label the drop is often about not having.
SELF_INSTRUMENTS: Final[tuple[InstrumentSpec, ...]] = (
    InstrumentSpec(
        name="observability_dropped_total",
        kind=InstrumentKind.COUNTER,
        description="Metric recordings this registry refused, by instrument and reason.",
        decision=(
            "Whether a flat line on a dashboard means nothing happened or means the call "
            "site is wrong. Rising 'unregistered' or 'unknown_label' is a bug in a caller; "
            "rising 'cardinality' means a label is carrying an id it should not."
        ),
        labels=("instrument", "reason"),
        tenant_scoped=False,
    ),
    InstrumentSpec(
        name="observability_errors_total",
        kind=InstrumentKind.COUNTER,
        description="Exceptions caught inside this package and not raised to the caller.",
        decision=(
            "Whether the observability layer is silently broken. It is built never to raise "
            "into commerce, so this counter is the only evidence that it failed at all."
        ),
        labels=("operation",),
        tenant_scoped=False,
    ),
    InstrumentSpec(
        name="observability_sink_failures_total",
        kind=InstrumentKind.COUNTER,
        description="Exceptions raised by an attached metrics sink and swallowed here.",
        decision=(
            "Whether an exporter is down. Commerce is unaffected either way, which is the "
            "design; this says whether the numbers are reaching anywhere."
        ),
        labels=("sink",),
        tenant_scoped=False,
    ),
)


class MetricsRegistry:
    """Instruments, their values, and a Prometheus rendering of both.

    Construct one per process. Register the catalogue you intend to expose -- yours, this
    package's, or another package's -- and hand out :class:`TenantMetrics` handles from
    :meth:`for_tenant`. Nothing here starts a thread, opens a socket or touches the disk.

    Thread-safe by one lock around every mutation. The critical sections are a dictionary
    lookup and an addition, so contention is not a consideration at any rate this platform
    will produce, and a lock is not a background thread.

    Every parameter of every recording method is **positional-only**. That is not a style
    preference: labels arrive as ``**kwargs``, so any named parameter would claim a label
    name for itself, and an instrument that legitimately has a label called ``value`` or
    ``amount`` would silently record into the wrong argument. With none of them nameable,
    no label name can collide with one.
    """

    __slots__ = ("_histograms", "_instruments", "_lock", "_reentrant", "_scalars", "_sinks")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._instruments: dict[str, InstrumentSpec] = {}
        self._scalars: dict[str, dict[tuple[str, ...], float]] = {}
        self._histograms: dict[str, dict[tuple[str, ...], _HistogramState]] = {}
        self._sinks: list[_SinkState] = []
        self._reentrant = False
        self.register_all(SELF_INSTRUMENTS)

    # ------------------------------------------------------------- registration

    def register(self, spec: InstrumentSpec) -> None:
        """Add an instrument. Idempotent for an identical spec; refuses a conflicting one.

        This is the extension point. A package owning instruments of its own -- the voice
        session's frame counters, queue depth, reconnect count, echo-gate engagement time
        and barge-in count (specification 19.13); the protocol layer's message counts --
        defines its own tuple of specs and calls this. It does not edit this package, and
        this package does not import it.
        """
        with self._lock:
            existing = self._instruments.get(spec.name)
            if existing is not None:
                if existing != spec:
                    raise SpecConflictError(
                        f"{spec.name!r} is already registered with a different definition; "
                        "two instruments cannot share a name and mean different things"
                    )
                return
            self._instruments[spec.name] = spec
            if spec.kind is InstrumentKind.HISTOGRAM:
                self._histograms[spec.name] = {}
            else:
                self._scalars[spec.name] = {}

    def register_all(self, specs: Iterable[InstrumentSpec]) -> None:
        """:meth:`register` for a whole catalogue."""
        for spec in specs:
            self.register(spec)

    def specs(self) -> tuple[InstrumentSpec, ...]:
        """Every registered spec, name-ordered. The catalogue as it actually stands."""
        with self._lock:
            return tuple(sorted(self._instruments.values(), key=lambda spec: spec.name))

    def spec(self, name: str) -> InstrumentSpec | None:
        """One spec by name, or ``None``."""
        with self._lock:
            return self._instruments.get(name)

    # ------------------------------------------------------------------- sinks

    def add_sink(self, sink: MetricsSink, *, name: str | None = None) -> None:
        """Attach a sink. See :class:`MetricsSink` for what one is allowed to do."""
        with self._lock:
            self._sinks.append(_SinkState(sink=sink, name=name or type(sink).__name__))

    def quarantined_sinks(self) -> tuple[str, ...]:
        """Sinks dropped after :data:`SINK_FAILURE_LIMIT` consecutive failures."""
        with self._lock:
            return tuple(state.name for state in self._sinks if state.quarantined)

    # --------------------------------------------------------------- recording

    def for_tenant(self, tenant_id: object) -> TenantMetrics:
        """A handle that writes only this tenant's series.

        The returned object has no argument that reaches another tenant's series, which is
        the isolation property in structural rather than intended form.
        """
        return TenantMetrics(self, _label_value(tenant_id))

    def for_current_scope(self) -> TenantMetrics:
        """A handle for the tenant in the bound correlation scope.

        The bridge between :mod:`platform_observability.correlation` and this module, and
        the reason a service deep in a call chain needs no tenant parameter. With no tenant
        bound, the handle is inert: its recordings are dropped and counted as
        ``missing_tenant`` rather than attributed to a guess.
        """
        tenant = current_tenant()
        return TenantMetrics(self, None if tenant is None else _label_value(tenant))

    def increment(self, name: str, amount: float = 1.0, /, **labels: object) -> None:
        """Add to a counter. Never raises; returns nothing.

        Only for instruments with ``tenant_scoped=False``. A tenant-scoped counter reached
        this way is dropped as ``missing_tenant``, because guessing which tenant a payment
        belonged to is worse than not counting it.
        """
        self._record(name, InstrumentKind.COUNTER, amount, None, labels)

    def set_gauge(self, name: str, value: float, /, **labels: object) -> None:
        """Set a gauge to an absolute value. Never raises; returns nothing."""
        self._record(name, InstrumentKind.GAUGE, value, None, labels, absolute=True)

    def observe(self, name: str, value: float, /, **labels: object) -> None:
        """Record a histogram observation. Never raises; returns nothing."""
        self._record(name, InstrumentKind.HISTOGRAM, value, None, labels)

    # -------------------------------------------------------------- reading back

    def value(self, name: str, /, **labels: object) -> float | None:
        """A counter's or gauge's current value, or ``None`` if that series has none.

        For tests, and for a health endpoint wanting one number. The full label set is
        required, ``tenant`` included: there is no way to ask for "the value across
        tenants", because that is the aggregation Prometheus exists to do and would be a
        route to a series the caller was never handed.
        """
        spec = self.spec(name)
        if spec is None or spec.kind is InstrumentKind.HISTOGRAM:
            return None
        key = self._read_key(spec, labels)
        if key is None:
            return None
        with self._lock:
            return self._scalars[name].get(key)

    def histogram(self, name: str, /, **labels: object) -> HistogramSnapshot | None:
        """One histogram series, cumulated, or ``None`` if it has no observations."""
        spec = self.spec(name)
        if spec is None or spec.kind is not InstrumentKind.HISTOGRAM:
            return None
        key = self._read_key(spec, labels)
        if key is None:
            return None
        with self._lock:
            state = self._histograms[name].get(key)
        return None if state is None else _snapshot(spec, state)

    def series(self, name: str) -> tuple[tuple[tuple[str, str], ...], ...]:
        """Every label combination recorded for an instrument. For assertions."""
        spec = self.spec(name)
        if spec is None:
            return ()
        with self._lock:
            store = self._histograms if spec.kind is InstrumentKind.HISTOGRAM else self._scalars
            keys = tuple(store[name])
        return tuple(sorted(_pairs(spec, key) for key in keys))

    # ------------------------------------------------------------------ render

    def render(self) -> str:
        """The Prometheus 0.0.4 text exposition of everything registered.

        **Every registered instrument gets its ``# HELP`` and ``# TYPE`` even with no
        series.** That is deliberate. Specification 19.13 says a counter that is never
        surfaced is not observability, and an instrument that appears in the exposition
        only after it first fires is invisible on precisely the day someone goes looking --
        the day it has *not* fired and they need to know whether that means "healthy" or
        "never wired up". The description is in the output too, so the exposition is the
        catalogue.

        Deterministic: instruments name-ordered, series label-ordered, so a diff between
        two scrapes is a diff in the numbers.
        """
        lines: list[str] = []
        with self._lock:
            for spec in sorted(self._instruments.values(), key=lambda item: item.name):
                lines.append(f"# HELP {spec.name} {_escape_help(spec.description)}")
                lines.append(f"# TYPE {spec.name} {spec.kind.value}")
                if spec.kind is InstrumentKind.HISTOGRAM:
                    lines.extend(_render_histogram(spec, self._histograms[spec.name]))
                else:
                    lines.extend(_render_scalar(spec, self._scalars[spec.name]))
        return "\n".join(lines) + "\n"

    # -------------------------------------------------------------- internals

    def _record(
        self,
        name: str,
        kind: InstrumentKind,
        value: float,
        tenant: str | None,
        labels: Mapping[str, object],
        *,
        absolute: bool = False,
    ) -> None:
        """The one path every recording takes. Total: it catches everything it can raise."""
        try:
            spec = self._instruments.get(name)
            if spec is None:
                self._drop(name, DropReason.UNREGISTERED)
                return
            if spec.kind is not kind:
                self._drop(name, DropReason.WRONG_KIND)
                return
            if not math.isfinite(value) or (kind is InstrumentKind.COUNTER and value < 0):
                # A counter that can go down is a gauge; a negative delta here is always an
                # arithmetic bug upstream rather than an intent.
                self._drop(name, DropReason.BAD_VALUE)
                return
            key = self._write_key(spec, labels, tenant)
            if key is None:
                return
            sample = self._store(spec, key, value, absolute=absolute)
            if sample is not None:
                self._dispatch(sample)
        except Exception as exc:  # pragma: no cover - the guard of last resort
            self._error("record", exc)

    def _write_key(
        self, spec: InstrumentSpec, labels: Mapping[str, object], tenant: str | None
    ) -> tuple[str, ...] | None:
        """Turn a keyword label mapping into an ordered value tuple, or refuse it."""
        if spec.tenant_scoped:
            if TENANT_LABEL in labels:
                self._drop(spec.name, DropReason.TENANT_OVERRIDE)
                return None
            if tenant is None:
                self._drop(spec.name, DropReason.MISSING_TENANT)
                return None
        declared = set(spec.labels)
        for supplied in labels:
            if supplied not in declared:
                self._drop(spec.name, DropReason.UNKNOWN_LABEL)
                return None
        # A declared label the caller omitted becomes the empty string, which is what a
        # Prometheus client does and what keeps every series of one instrument the same
        # shape -- a requirement of the exposition, not a nicety.
        values = tuple(_label_value(labels.get(label, "")) for label in spec.labels)
        if spec.tenant_scoped and tenant is not None:
            return (tenant, *values)
        return values

    def _read_key(
        self, spec: InstrumentSpec, labels: Mapping[str, object]
    ) -> tuple[str, ...] | None:
        """The key for a read. Unlike a write, ``tenant`` is a normal keyword here."""
        for supplied in labels:
            if supplied not in spec.label_names:
                return None
        return tuple(_label_value(labels.get(label, "")) for label in spec.label_names)

    def _store(
        self, spec: InstrumentSpec, key: tuple[str, ...], value: float, *, absolute: bool
    ) -> Sample | None:
        with self._lock:
            if spec.kind is InstrumentKind.HISTOGRAM:
                histograms = self._histograms[spec.name]
                state = histograms.get(key)
                if state is None:
                    if len(histograms) >= MAX_SERIES_PER_INSTRUMENT:
                        self._bump_locked_drop(spec.name, DropReason.CARDINALITY)
                        return None
                    state = _HistogramState(counts=[0] * (len(spec.buckets or ()) + 1))
                    histograms[key] = state
                state.counts[_bucket_index(spec.buckets or (), value)] += 1
                state.total += value
                state.observations += 1
                return Sample(spec.name, spec.kind, _pairs(spec, key), value)

            scalars = self._scalars[spec.name]
            if key not in scalars and len(scalars) >= MAX_SERIES_PER_INSTRUMENT:
                self._bump_locked_drop(spec.name, DropReason.CARDINALITY)
                return None
            updated = value if absolute else scalars.get(key, 0.0) + value
            scalars[key] = updated
            return Sample(spec.name, spec.kind, _pairs(spec, key), updated)

    def _dispatch(self, sample: Sample) -> None:
        """Hand a sample to every live sink, surviving anything any of them does."""
        if not self._sinks:
            return
        with self._lock:
            states = [state for state in self._sinks if not state.quarantined]
        for state in states:
            try:
                state.sink.observe(sample)
            except Exception as exc:
                self._sink_failed(state, exc)
            else:
                state.consecutive_failures = 0

    def _sink_failed(self, state: _SinkState, exc: BaseException) -> None:
        state.consecutive_failures += 1
        self._bump("observability_sink_failures_total", {"sink": state.name})
        if state.consecutive_failures >= SINK_FAILURE_LIMIT and not state.quarantined:
            state.quarantined = True
            _LOG.error(
                "metrics sink %s quarantined after %d consecutive failures: %s",
                state.name,
                state.consecutive_failures,
                type(exc).__name__,
            )

    def _drop(self, instrument: str, reason: DropReason) -> None:
        self._bump(
            "observability_dropped_total", {"instrument": instrument, "reason": reason.value}
        )

    def _bump_locked_drop(self, instrument: str, reason: DropReason) -> None:
        """:meth:`_drop` from inside the lock. Same counter, no second acquisition."""
        self._bump_locked(
            "observability_dropped_total", {"instrument": instrument, "reason": reason.value}
        )

    def _error(self, operation: str, exc: BaseException) -> None:
        self._bump("observability_errors_total", {"operation": operation})
        if not self._reentrant:
            # A failing log handler must not re-enter this method through its own
            # exception. The flag is not locked: the worst a race costs is a duplicate
            # warning line, and a lock here could deadlock against a handler that logs.
            self._reentrant = True
            try:
                _LOG.warning(
                    "observability recording failed in %s: %s", operation, type(exc).__name__
                )
            finally:
                self._reentrant = False

    def _bump(self, name: str, labels: Mapping[str, str]) -> None:
        with self._lock:
            self._bump_locked(name, labels)

    def _bump_locked(self, name: str, labels: Mapping[str, str]) -> None:
        """Increment one of :data:`SELF_INSTRUMENTS` directly.

        Deliberately not routed through :meth:`_record`: a drop reported through the path
        that reports drops is a recursion waiting for one bad label. These counters are
        also not dispatched to sinks, so a failing sink cannot make its own failure counter
        fail.
        """
        spec = self._instruments.get(name)
        if spec is None:  # pragma: no cover - SELF_INSTRUMENTS is registered in __init__
            return
        series = self._scalars[name]
        key = tuple(_label_value(labels.get(label, "")) for label in spec.labels)
        if key not in series and len(series) >= MAX_SERIES_PER_INSTRUMENT:
            return
        series[key] = series.get(key, 0.0) + 1.0


class TenantMetrics:
    """A recording handle bound to one tenant, and the only way to write a tenant series.

    Obtained from :meth:`MetricsRegistry.for_tenant` or
    :meth:`MetricsRegistry.for_current_scope`. No method here takes an argument that can
    reach a different tenant's series: the tenant is the handle's own state, and a
    ``tenant=`` keyword passed as a label is refused and counted as ``tenant_override``
    rather than honoured.

    Instruments with ``tenant_scoped=False`` work through this handle too, so a component
    holding one does not need a second object for the process-wide ones.
    """

    __slots__ = ("_registry", "_tenant")

    def __init__(self, registry: MetricsRegistry, tenant: str | None) -> None:
        self._registry = registry
        self._tenant = tenant

    @property
    def tenant(self) -> str | None:
        """The tenant this handle writes, or ``None`` for an inert handle."""
        return self._tenant

    def increment(self, name: str, amount: float = 1.0, /, **labels: object) -> None:
        """Add to a counter under this tenant. Never raises; returns nothing."""
        self._registry._record(name, InstrumentKind.COUNTER, amount, self._tenant, labels)

    def set_gauge(self, name: str, value: float, /, **labels: object) -> None:
        """Set a gauge under this tenant. Never raises; returns nothing."""
        self._registry._record(
            name, InstrumentKind.GAUGE, value, self._tenant, labels, absolute=True
        )

    def add_gauge(self, name: str, delta: float, /, **labels: object) -> None:
        """Move a gauge by a delta -- for a depth that goes up and down. Never raises."""
        self._registry._record(name, InstrumentKind.GAUGE, delta, self._tenant, labels)

    def observe(self, name: str, value: float, /, **labels: object) -> None:
        """Record a histogram observation under this tenant. Never raises."""
        self._registry._record(name, InstrumentKind.HISTOGRAM, value, self._tenant, labels)


# --------------------------------------------------------------------- helpers


def _label_value(value: object) -> str:
    """A label value: text, scrubbed, single-line and capped.

    Scrubbed through the same redactor as a log field, because a label is every bit as
    exported as a log line and ``reason="Bearer eyJ..."`` is the same disclosure in a
    different file. Newlines are replaced rather than escaped: a label value containing a
    line break is a rendering hazard for every consumer of the text format.
    """
    text = scrub_text(str(value)).replace("\n", " ").replace("\r", " ")
    return text[:MAX_LABEL_VALUE_LENGTH]


def _pairs(spec: InstrumentSpec, key: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(zip(spec.label_names, key, strict=True))


def _bucket_index(buckets: tuple[float, ...], value: float) -> int:
    """The first bucket whose upper bound the value fits, else the implicit ``+Inf``."""
    for index, bound in enumerate(buckets):
        if value <= bound:
            return index
    return len(buckets)


def _snapshot(spec: InstrumentSpec, state: _HistogramState) -> HistogramSnapshot:
    cumulative = 0
    pairs: list[tuple[float, int]] = []
    for index, bound in enumerate(spec.buckets or ()):
        cumulative += state.counts[index]
        pairs.append((bound, cumulative))
    return HistogramSnapshot(buckets=tuple(pairs), count=state.observations, total=state.total)


def _escape_help(text: str) -> str:
    """``# HELP`` escaping: backslash and newline only, per the exposition format."""
    return text.replace("\\", r"\\").replace("\n", r"\n")


def _escape_label(text: str) -> str:
    """Label-value escaping: backslash, double quote and newline."""
    return text.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _label_block(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
    parts = [f'{name}="{_escape_label(value)}"' for name, value in zip(names, values, strict=True)]
    if extra:
        parts.insert(0, extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def _format_number(value: float) -> str:
    """A count of whole things renders as an integer; everything else keeps its float."""
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _format_bound(bound: float) -> str:
    return "+Inf" if math.isinf(bound) else repr(float(bound))


def _render_scalar(spec: InstrumentSpec, series: Mapping[tuple[str, ...], float]) -> list[str]:
    return [
        f"{spec.name}{_label_block(spec.label_names, key)} {_format_number(value)}"
        for key, value in sorted(series.items())
    ]


def _render_histogram(
    spec: InstrumentSpec, series: Mapping[tuple[str, ...], _HistogramState]
) -> list[str]:
    """Buckets, then sum, then count -- cumulative, with ``+Inf`` equal to ``_count``.

    The ``+Inf`` bucket is not optional and must equal ``_count``. A scrape where it does
    not is rejected outright by some consumers and silently mis-quantiled by others, which
    is the worse of the two.
    """
    lines: list[str] = []
    for key, state in sorted(series.items()):
        cumulative = 0
        for index, bound in enumerate(spec.buckets or ()):
            cumulative += state.counts[index]
            block = _label_block(spec.label_names, key, extra=f'le="{_format_bound(bound)}"')
            lines.append(f"{spec.name}_bucket{block} {cumulative}")
        inf_block = _label_block(spec.label_names, key, extra='le="+Inf"')
        lines.append(f"{spec.name}_bucket{inf_block} {state.observations}")
        labels = _label_block(spec.label_names, key)
        lines.append(f"{spec.name}_sum{labels} {_format_number(state.total)}")
        lines.append(f"{spec.name}_count{labels} {state.observations}")
    return lines
