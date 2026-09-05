"""Operational telemetry for the platform: metrics, redacting logs, correlation, timing.

The distinction this package is built around
--------------------------------------------
An **audit event is evidence**. ``transaction_kernel.audit`` writes it in the same
transaction as the state change it records, hash-chained to its predecessor, gapless,
tenant-scoped, and it has to be exactly right. Losing one is a corruption incident.

A **metric is a gauge**. It is cheap, lossy, aggregated, capped, and it disappears on
restart. Losing one must never matter.

Keeping those apart is the entire point of this package existing separately from the
kernel. Two rules follow and neither of them bends:

1. **A counter is never the record of a money action.** ``commerce_refunds_open`` says how
   many refunds are waiting; the refund is the row in ``refunds`` and the chain in
   ``audit_events``. If a question about a buyer's money can only be answered from a
   metric, the answer is wrong.
2. **Nothing here is on the path that decides whether money moves.** That is structural,
   not aspirational, and it is enforced three ways:

   * this package has **no dependencies** -- not on ``transaction-kernel``, not on
     ``commerce-domain``, not on anything outside the standard library, which is asserted
     by a test that reads every import in the source tree;
   * every recording call returns ``None`` and catches its own exceptions, so no caller
     can branch on whether a metric was recorded, and no failure here can propagate into
     one;
   * there is no network client, no push, no background thread and no disk access. The
     registry renders text; whoever mounts it owns the endpoint. If the metrics backend is
     down, this package does not find out and commerce does not notice.

What is in here
---------------
:mod:`~platform_observability.metrics`
    A tenant-labelled registry of counters, gauges and histograms, and a Prometheus text
    exposition of them. Instruments are registered by whoever owns them, so a package can
    add its own without editing this one.

:mod:`~platform_observability.instruments`
    The platform's own instruments as data -- each with its labels and a sentence naming
    the decision it informs, because a counter nobody surfaces is not observability
    (specification 19.13).

:mod:`~platform_observability.logs`
    One JSON line per event, carrying the correlation scope, with redaction applied at
    format time so it also covers code that has never heard of this package.

:mod:`~platform_observability.redaction`
    The types and rules that make a token, a card number, a signature or a whole webhook
    body *unrepresentable* in a log line rather than merely absent from today's.

:mod:`~platform_observability.correlation`
    A context variable carrying the correlation id across an ``await``, so a log line, a
    span and an audit row can be joined without threading a parameter through every
    function.

:mod:`~platform_observability.timing`
    A span helper recording duration and outcome, with an OpenTelemetry-shaped seam and no
    OpenTelemetry dependency.

Mounting it is deliberately not done here. ``docs/adr/0007-observability.md`` has the
exact import, middleware and endpoint for the API and the worker.
"""

from __future__ import annotations

from .correlation import (
    CORRELATION_ID_HEADER,
    Scope,
    bind_scope,
    correlation_id,
    correlation_id_from_header,
    current_scope,
    current_tenant,
    new_correlation_id,
    scope_fields,
)
from .instruments import (
    ADMISSION_TIMING,
    PLATFORM_INSTRUMENTS,
    PROVIDER_TIMING,
    WORKER_COMMAND_TIMING,
    default_registry,
    reset_default_registry,
)
from .logs import (
    CorrelationFilter,
    EventLogger,
    JsonFormatter,
    configure_logging,
)
from .metrics import (
    AGE_BUCKETS,
    LATENCY_BUCKETS,
    PROMETHEUS_CONTENT_TYPE,
    SELF_INSTRUMENTS,
    DropReason,
    HistogramSnapshot,
    InstrumentKind,
    InstrumentSpec,
    MetricsRegistry,
    MetricsSink,
    Sample,
    SpecConflictError,
    TenantMetrics,
)
from .redaction import REDACTED, LogValue, Secret, redact_fields, redact_value, scrub_text
from .timing import Outcome, Span, SpanHandle, Timing, Tracer, set_tracer, timed, tracer

__all__ = [
    "ADMISSION_TIMING",
    "AGE_BUCKETS",
    "CORRELATION_ID_HEADER",
    "LATENCY_BUCKETS",
    "PLATFORM_INSTRUMENTS",
    "PROMETHEUS_CONTENT_TYPE",
    "PROVIDER_TIMING",
    "REDACTED",
    "SELF_INSTRUMENTS",
    "WORKER_COMMAND_TIMING",
    "CorrelationFilter",
    "DropReason",
    "EventLogger",
    "HistogramSnapshot",
    "InstrumentKind",
    "InstrumentSpec",
    "JsonFormatter",
    "LogValue",
    "MetricsRegistry",
    "MetricsSink",
    "Outcome",
    "Sample",
    "Scope",
    "Secret",
    "Span",
    "SpanHandle",
    "SpecConflictError",
    "TenantMetrics",
    "Timing",
    "Tracer",
    "bind_scope",
    "configure_logging",
    "correlation_id",
    "correlation_id_from_header",
    "current_scope",
    "current_tenant",
    "default_registry",
    "new_correlation_id",
    "redact_fields",
    "redact_value",
    "reset_default_registry",
    "scope_fields",
    "scrub_text",
    "set_tracer",
    "timed",
    "tracer",
]
