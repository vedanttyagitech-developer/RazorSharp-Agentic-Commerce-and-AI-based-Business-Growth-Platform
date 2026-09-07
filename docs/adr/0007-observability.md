# ADR 0007: The observability layer

Status: accepted, 2026-09-05. Implements specification 19.13 and the operational half of
sections 22, 24.3 and 26. Extends ADR 0003. Where this file and
`packages/platform-observability` disagree, this file is what is built.

## Context

The platform has an evidential telemetry system already and it is excellent at its job.
`transaction_kernel.audit` writes a row in the same transaction as the state change it
records, hash-chains it to its predecessor, scopes it to a tenant, and refuses to be
edited. `commerce_api.deps` mints or adopts a correlation id per request and carries it
into `AgentPrincipal`, every outbox command and every audit row. The Money Action Proof
Chain (spec 26.4) reconstructs any payment from those rows.

None of that answers an operational question. "Is the worker keeping up", "is Razorpay
slow tonight", "why did submissions stop three minutes ago" are not questions the audit
stream should be asked, and every attempt to ask them of it ends the same way: somebody
writes a query over `audit_events`, it becomes a dashboard, the dashboard becomes load on
the table that money writes depend on, and the evidential system is now on the critical
path of a Grafana refresh.

So there are two systems, and the whole design follows from keeping them apart.

|                | Audit event                          | Metric                          |
| -------------- | ------------------------------------ | ------------------------------- |
| What it is     | Evidence                             | A gauge                         |
| Guarantees     | Gapless, hash-chained, tenant-scoped | Cheap, lossy, aggregated        |
| Losing one     | A corruption incident                | A non-event                     |
| Written        | In the transaction it describes      | Anywhere, or not at all         |
| Lives          | Forever, in PostgreSQL               | Until the process restarts      |
| Read by        | A dispute, a regulator, a verifier   | An operator, at 3am             |

Two rules fall out and neither of them bends:

1. **A counter is never the record of a money action.** `commerce_refunds_open` says how
   many refunds are waiting. The refund is the row in `refunds` and its chain in
   `audit_events`. If a question about a buyer's money can only be answered from a metric,
   the answer is wrong.
2. **Nothing in this layer is on the path that decides whether money moves.**

## Decisions

### D1. A separate package with no dependencies at all

`packages/platform-observability` depends on nothing — not `transaction-kernel`, not
`commerce-domain`, nothing outside the standard library. `test_po_boundary` reads every
import in the source tree with `ast` and fails on anything that is not in
`sys.stdlib_module_names`.

That is stricter than ADR 0003 D2's "no cycles", and deliberately so. The moment this
package imports `transaction_kernel` for a convenient `RecoveryCode`, the kernel has an
observability layer in its reverse-dependency graph, and a change here can break a money
path. Reason codes therefore appear as *strings in label values*. The duplication is a
few enum names; what it buys is that no edit to this package can affect a payment.

### D2. A registry that renders, and never pushes

`MetricsRegistry` holds counters, gauges and histograms and renders Prometheus 0.0.4 text.
There is no HTTP client, no push, no background thread, no disk access. A registry that
pushes has a socket, a timeout, a retry policy and a queue, and every one of those can
block a request thread while a payment is in flight.

Whoever mounts it owns the endpoint. A scrape that fails is Prometheus's problem.

`MetricsSink` is the seam for anyone who does want a push exporter: one method, called
synchronously, wrapped in a guard, and **quarantined after five consecutive failures** so
a dead backend costs one `try` per recording rather than a timeout per recording.

### D3. Every recording is total and returns `None`

No public recording method raises, and none returns anything. Both halves matter: the
first means a broken metric cannot fail a payment, and the second means no caller can come
to *depend* on a metric having been recorded, because there is nothing to branch on. What
was refused and why is counted on `observability_dropped_total`, so the layer explains its
own silences.

### D4. The tenant label is not a parameter

A tenant-scoped instrument can only be written through `registry.for_tenant(id)`. The
handle carries the tenant as its own state; a `tenant=` keyword passed as a label is
refused and counted as `tenant_override`. A component holding tenant A's handle has no
argument it can supply that reaches tenant B's series. This is the structural equivalent
of what row-level security gives the database.

Reads require the full label set, tenant included: there is no "value across tenants",
because that is the aggregation Prometheus exists to perform.

### D5. Cardinality is capped, and identifiers are never labels

2,000 series per instrument; past that, new label combinations are dropped and counted.
No instrument in the catalogue has a label that could carry a checkout id, an order id or
a buyer reference — asserted by `test_no_label_could_carry_an_identifier`. HTTP metrics
label the **route template**, never the resolved path: a resolved path is unbounded
cardinality *and* a buyer's checkout id in an exported series that outlives the request by
weeks.

### D6. Redaction is structural, in four independent layers

Matching the posture `commerce_api.errors` already takes for HTTP responses (a 5xx never
carries `str(exc)`; an `IntegrityError` yields its constraint *name* and never its SQL).

1. **The type.** `LogValue = str | int | float | bool | None`. A dict, a list of basket
   items or raw `bytes` is not assignable to it, so `log.info(e, body=payload)` fails
   `mypy --strict` at the call site. This is the layer that turns "must not" into "cannot".
2. **The runtime type check.** A non-scalar that arrives anyway becomes
   `[redacted:dict sha256=…]`. The digest is the same SHA-256 `webhook_inbox.body_digest`
   stores, so the join to the evidence survives without the body ever being in the log.
3. **The field name.** `token`, `signature`, `card`, `secret`, `body`, `authorization`,
   `email`, `phone`, `upi` and twenty more are refused by name whatever they hold.
   `ALLOWED_FIELD_NAMES` rescues the handful that contain a denied fragment and are safe:
   `body_digest`, `self_hash`, `prev_hash`, `receipt_hash`.
4. **The value's shape.** Luhn-valid card numbers, `Bearer`/`Basic` credentials, PEM
   private keys and `rzp_test_`/`rzp_live_` credentials are removed from any string —
   including the rendered message of a `LogRecord` emitted by a package that has never
   heard of this one. That is the layer that catches
   `_log.debug("headers=%s", request.headers)` in code we do not own.

A 64-character hex string is **not** scrubbed by shape, because it is both an HMAC
signature and a SHA-256 audit hash, and taking the audit chain's own links out of the logs
would make a hash chain uninvestigable (spec 26.2). Signatures are excluded by name
instead. Provider references (`pay_…`, `order_…`, `rfnd_…`) pass through: they are links
in the Money Action Proof Chain and are exactly what an operator needs.

`Secret[T]` wraps a value that must be carried but never rendered: `str`, `repr` and
`__format__` all give `[redacted]`, so no f-string can get the value out. `reveal()` is
the only way, and it is one word to grep for at review.

### D7. One JSON line per event, fields nested

```json
{"ts":"2026-09-05T18:41:02.481913Z","level":"INFO","logger":"action_executor.loop",
 "event":"worker.command.completed","message":"…","correlation_id":"01a06f…",
 "tenant_id":"…","actor_type":"WORKER","fields":{"command_type":"REFUND_EXECUTE"}}
```

Fields are nested under `fields` rather than spread at the top level, and that is a
redaction property rather than a style choice: a caller passing `level="urgent"` or `ts=0`
cannot overwrite the envelope. Event names are validated against
`^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$`; an invalid one becomes the constant
`invalid_event_name` with the offender kept in a field, because a log line is never worth
raising over.

Redaction happens in `JsonFormatter.format`, not in `EventLogger`. That is what makes it
cover uvicorn, SQLAlchemy, `commerce_api.errors` and everything else in the process.

### D8. Correlation lives in a `ContextVar`

`bind_scope()` binds correlation id, tenant, actor type and causation id for a block and
restores by token on exit. A value set before an `await` survives it; `asyncio.create_task`
copies the context, so a request fanning out to three calls gives three tasks reporting
the same id and none that can corrupt the parent's. A **thread** does not inherit it,
which is honest: the worker binds its own scope per leased command, and silently
inheriting an HTTP request's id there would be a lie about causation.

The id is not authority — the same thing `deps.py` says about the header. Nothing in this
package reads it to decide anything.

### D9. A tracer-shaped seam, with no OpenTelemetry dependency

Specification 22 names OpenTelemetry as the eventual stack. `timed()` records a duration
histogram and an outcome counter, and offers the span to a `Tracer` protocol two methods
wide; the default does nothing. An adapter over `opentelemetry.trace` is a few lines and
belongs in whatever package owns that dependency, not in this one (D1).

**When a `Timing` has an outcome counter, the outcome label goes on the histogram too**,
and both instruments must declare it. Failures are usually fast, so a latency histogram
that mixes them in flatters its own percentiles and hides the slow successes that are the
actual problem.

There are no Prometheus **exemplars**: exemplars are an OpenMetrics feature and this
renders the 0.0.4 text format, where there is nowhere to put one. The trace/metric join is
made in the log store and in the tracer, both of which carry the correlation id the audit
rows carry.

### D10. Every instrument names the decision it informs

`InstrumentSpec` will not construct without a `decision` string. Specification 19.13:
"Counters that are never surfaced are not observability." An instrument nobody can name a
decision for is that failure one step earlier — it gets surfaced on a dashboard nobody
reads, because nobody knows what seeing it should make them do.

`render()` emits `# HELP` and `# TYPE` for **every registered instrument even at zero
series**, so a scrape of a freshly started process lists the whole catalogue with its help
text. That is what makes "no data" and "not wired up" distinguishable on day one rather
than during the first incident.

### D11. Registration is open

`registry.register(spec)` is idempotent for an identical spec and raises `SpecConflictError`
for a conflicting one — at import or mount time, never on a request. A package owning
instruments of its own defines them beside its own code and registers them; it does not
edit `platform_observability`, and `platform_observability` does not import it. See
"For the voice and protocol sessions" below.

## The catalogue

Namespaces: `commerce_` for the platform, `observability_` for the registry's own,
whatever you like for yours. Every `commerce_` instrument is tenant-scoped.

| Area | Instruments |
| --- | --- |
| HTTP | `commerce_http_requests_total{route,method,status}`, `commerce_http_request_duration_seconds{route,method}` |
| Admission | `commerce_admissions_total{operation,outcome}`, `commerce_admission_denials_total{operation,code}`, `commerce_admission_duration_seconds{operation}` |
| Safe Mode | `commerce_safe_mode_engaged{scope,mode}`, `commerce_safe_mode_blocks_total{operation,mode}` |
| Grants | `commerce_execution_grants_{issued,consumed,expired}_total{operation}`, `commerce_execution_grants_revoked_total{operation,cause}` |
| Outbox | `commerce_outbox_depth{status,command_type}`, `commerce_outbox_oldest_pending_age_seconds{command_type}` |
| Worker | `commerce_worker_leases_total{command_type}`, `commerce_worker_attempts_total{command_type,outcome}`, `commerce_worker_dead_letters_total{command_type,code}`, `commerce_worker_command_duration_seconds{command_type,outcome}` |
| Provider | `commerce_provider_requests_total{provider,operation,outcome}`, `commerce_provider_request_duration_seconds{provider,operation,outcome}`, `commerce_provider_retries_total{provider,operation,reason}` |
| Webhooks | `commerce_webhook_deliveries_total{event_type,disposition}`, `commerce_webhook_apply_lag_seconds{event_type}` |
| Reconciliation | `commerce_reconciliation_runs_total{kind,conclusion}`, `commerce_reconciliation_findings_total{kind,finding}`, `commerce_payments_unresolved{state}` |
| Refunds | `commerce_refund_requests_total{outcome,code}`, `commerce_refunds_open{wire_state}`, `commerce_refund_escalations_total{reason}` |
| Audit | `commerce_audit_appends_total{aggregate_type}`, `commerce_audit_append_conflicts_total{aggregate_type}`, `commerce_audit_chain_verifications_total{verdict}` |
| Self | `observability_dropped_total{instrument,reason}`, `observability_errors_total{operation}`, `observability_sink_failures_total{sink}` |

Duplicate webhooks are `disposition="duplicate"` on the deliveries counter rather than a
counter of their own, so accepted and duplicate always sum to what the provider sent.

The three numbers worth alerting on: `commerce_worker_dead_letters_total` increasing (the
platform has stopped trying and a buyer is owed an answer), `commerce_refunds_open{wire_state="UNKNOWN"}`
above zero (we do not know whether a buyer's money moved), and
`commerce_audit_chain_verifications_total{verdict!="intact"}` above zero (an integrity
incident). Each of those has its evidence elsewhere; the counter only says to go and look.

---

## How to mount it

**This is deliberately not wired in.** Three sessions were editing `apps/**`,
`commerce-api`, `voice-runtime` and `commerce-protocols` on the night this package was
written, and an edit of mine to those files would have been lost or would have broken
theirs. Everything below is the mechanical form of the wiring.

The API recipe was **run against a real FastAPI app in isolation** before being written
down, which is how the `BaseHTTPMiddleware` trap, the `request.state` carrier and the
`ContextVar.reset` bug below were found rather than shipped. It is worth doing that again
when it is wired in for real.

### The API — `commerce_api.app`

```python
import time

from platform_observability import (
    PROMETHEUS_CONTENT_TYPE, bind_scope, configure_logging, correlation_id_from_header,
    default_registry,
)
from platform_observability.correlation import CORRELATION_ID_HEADER

configure_logging()          # in the lifespan, once. Never at import.
REGISTRY = default_registry()
```

**Middleware — pure ASGI, not `BaseHTTPMiddleware`.** `BaseHTTPMiddleware` runs the
downstream app in a separate anyio task, so `scope["route"]` and the response status are
awkward to read back. A pure ASGI middleware runs the app in the same task and reads both
directly.

```python
class ObservabilityMiddleware:
    """Bind the correlation scope, then count and time the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation = correlation_id_from_header(Headers(scope=scope).get(CORRELATION_ID_HEADER))
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"].append(
                    (CORRELATION_ID_HEADER.lower().encode(), correlation.encode())
                )
            await send(message)

        started = time.perf_counter()
        with bind_scope(correlation):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                elapsed = time.perf_counter() - started
                # The route TEMPLATE, resolved by the router during dispatch, so it is read
                # after the app returns. Never scope["path"], which carries buyer ids (D5).
                route = getattr(scope.get("route"), "path", "unmatched")
                # A sentinel tenant rather than none: an unauthenticated 401 or a 404 has no
                # tenant, and dropping those would hide exactly the traffic an incident
                # starts with.
                tenant = scope.get("state", {}).get("tenant_id") or "unauthenticated"
                metrics = REGISTRY.for_tenant(tenant)
                metrics.observe(
                    "commerce_http_request_duration_seconds", elapsed,
                    route=route, method=scope["method"],
                )
                metrics.increment(
                    "commerce_http_requests_total",
                    route=route, method=scope["method"], status=f"{status // 100}xx",
                )

app.add_middleware(ObservabilityMiddleware)
```

**How the tenant reaches the middleware, and why it is not the context variable.** This is
the one part that is genuinely counter-intuitive and it was got wrong once before it was
verified. A context variable bound *downstream* — inside a dependency, inside the endpoint
— is **not** visible back in the middleware: FastAPI dispatches dependencies and sync
endpoints through anyio, under copied contexts, and a copy's writes never reach the
original. `request.state` is the carrier that does work, because `scope["state"]` is one
mutable dict shared by everything in the request.

So the dependency does both. `request.state` carries the tenant *out* to the middleware;
`bind_scope` carries it *down* to every log line and metric inside the request:

```python
# commerce_api.deps, beside require_session. ASYNC, deliberately -- see below.
async def observed_session(
    request: Request, ctx: SessionContext
) -> AsyncIterator[RequestContext]:
    request.state.tenant_id = str(ctx.tenant_id)
    with bind_scope(ctx.correlation_id, tenant_id=ctx.tenant_id,
                    actor_type=ctx.actor_type.value):
        yield ctx
```

**Write it `async def`.** A synchronous `yield` dependency is run through anyio's thread
pool, and FastAPI enters it and leaves it under two *different* copied contexts. That used
to make `ContextVar.reset` raise `ValueError: Token was created in a different Context`
out of the dependency's teardown — turning a deliberate 409 into a 500. `bind_scope` now
falls back to restoring the parent value by hand when its token is foreign
(`correlation._restore`, and `TestUnwindingInAForeignContext` covers it), so a sync
dependency is *safe*; it is still the wrong choice, because the scope it binds lives in a
worker thread's context and an `async def` endpoint will not see it.

**The endpoint.** Its owner's, not this package's:

```python
@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(REGISTRY.render(), media_type=PROMETHEUS_CONTENT_TYPE)
```

Put it behind the operator surface or on a separate port. The exposition carries no buyer
data by construction (D5), but it does describe the platform's shape and has no reason to
be on the buyer-facing route table.

**One honest limitation.** An exception that reaches Starlette's `ServerErrorMiddleware` —
which is where `install_error_handlers`' `Exception` handler ends up — is answered *above*
this middleware, so `send_wrapper` never runs and that response carries no
`X-Correlation-Id`. The request is still counted as `5xx`, because `status` defaults to
500. Every handled refusal, including every kernel denial and every RFC 9457 problem, goes
through `ExceptionMiddleware` below this one and does carry the header.

**Where the other API instruments are recorded.** `commerce_admissions_total` and
`commerce_admission_denials_total` from `admission_service`, off the `KernelDecision` the
kernel already returns — `decision.allowed` and `decision.code.value`, no new plumbing.
`commerce_refund_requests_total` from `refund_service` the same way.
`commerce_webhook_deliveries_total` from the webhook router, one increment per disposition
on the path ADR 0003 D7 already fixes.

### The Action Executor — `action_executor.main` and `action_executor.loop`

```python
from platform_observability import (
    EventLogger, bind_scope, configure_logging, default_registry, timed,
    WORKER_COMMAND_TIMING,
)

configure_logging()                    # in main(), once
REGISTRY = default_registry()
LOG = EventLogger("action_executor.loop")
```

Per leased command, in `_run_one` — the worker's threads do not inherit a scope (D8), so
each command binds its own from the correlation id the outbox row already carries:

```python
def _run_one(runtime: WorkerRuntime, leased: LeasedCommand) -> HandlerResult:
    metrics = REGISTRY.for_tenant(leased.tenant_id)
    with bind_scope(leased.correlation_id, tenant_id=leased.tenant_id, actor_type="WORKER"):
        metrics.increment("commerce_worker_leases_total", command_type=leased.command_type)
        with timed(metrics, WORKER_COMMAND_TIMING, command_type=leased.command_type) as span:
            try:
                result = dispatch(runtime, leased)
            except Exception as exc:
                span.set_outcome("failed")
                LOG.exception("worker.command.failed", command_type=leased.command_type)
                return HandlerResult(code=_code_for(exc), detail=reason_key(type(exc).__name__))
            span.set_outcome("completed" if result.completed else "failed")
            return result
```

The existing `_LOG.warning(...)` calls stay exactly as they are; `configure_logging()`
turns them into JSON with the correlation id attached, which is the point of D7.

Dead letters, in `_recorder_for` beside the `tk.append` that is already there — the audit
row remains the evidence and the counter only says to go and look at it:

```python
metrics.increment(
    "commerce_worker_dead_letters_total",
    command_type=letter.command_type,
    code=letter.terminal_code.value,
)
```

Outbox depth, from housekeeping, which already sweeps every tenant once a tick:

```python
for status, count in outbox_counts_by_status(session, tenant_id):
    metrics.set_gauge("commerce_outbox_depth", count, status=status.value, command_type="")
```

Provider calls, in `action_executor.transport` — the one place that talks to Razorpay
(ADR 0003 D3), so one call site covers every provider instrument:

```python
with timed(metrics, PROVIDER_TIMING, provider="razorpay", operation=op) as span:
    response = client.request(...)
    span.set_outcome(_classify(response))     # ok | client_error | server_error | timeout
```

**The worker has no HTTP server**, so it cannot be scraped. Two options, both the mounting
owner's call: run a `prometheus_client` push gateway sidecar reading `REGISTRY.render()`,
or add a thin `/metrics`-only ASGI app on a second port. Do not add a push client to this
package (D2).

### For the voice and protocol sessions

You do not edit `platform_observability`. Define your instruments beside your own code and
register them:

```python
# packages/voice-runtime/src/voice_runtime/telemetry.py
from platform_observability import InstrumentKind, InstrumentSpec, default_registry

VOICE_INSTRUMENTS = (
    InstrumentSpec(
        name="voice_frames_total",
        kind=InstrumentKind.COUNTER,
        description="Audio frames handled, by direction and disposition.",
        decision="Whether the queue is evicting under load, which is silent otherwise.",
        labels=("direction", "disposition"),   # inbound|outbound, sent|evicted|dropped
    ),
    InstrumentSpec(
        name="voice_queue_depth",
        kind=InstrumentKind.GAUGE,
        description="Frames waiting to be sent to the transcriber.",
        decision="Whether to widen the queue or evict earlier (spec 19.12).",
    ),
    InstrumentSpec(
        name="voice_stream_rotations_total",
        kind=InstrumentKind.COUNTER,
        description="STT stream rotations, by outcome.",
        decision="Whether rotation at the configured margin is losing audio.",
        labels=("outcome",),
    ),
    InstrumentSpec(
        name="voice_reconnects_total",
        kind=InstrumentKind.COUNTER,
        description="STT reconnects after a lost connection, by what followed.",
        decision="Whether buyers are being pushed to the text fallback (spec 19.12).",
        labels=("outcome",),
    ),
    InstrumentSpec(
        name="voice_echo_gate_engaged_seconds",
        kind=InstrumentKind.HISTOGRAM,
        description="Time the echo gate held input suppressed, per engagement.",
        decision="Whether suppression is costing the buyer their turn.",
        unit="seconds",
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    ),
    InstrumentSpec(
        name="voice_barge_ins_total",
        kind=InstrumentKind.COUNTER,
        description="Buyer interruptions of assistant speech, by what was cancelled.",
        decision="Whether cancellation is reaching in-flight synthesis before it is sent.",
        labels=("cancelled",),
    ),
)

default_registry().register_all(VOICE_INSTRUMENTS)
```

That covers every counter specification 19.13 names. Protocol message counts are the same
shape — `protocol_messages_total{protocol,version,direction,outcome}` and
`protocol_verification_failures_total{protocol,reason}` — with `protocol` as the namespace.

Two things to hold to. Use `bind_scope` once per voice session so **one correlation id
reconstructs the whole conversation across every stream rotation** (19.13), which is the
requirement a rotation would otherwise break. And log a swallowed callback exception with
`EventLogger.exception`, never `debug` — 19.13 again, and the reason the `debug` shortcut
is not offered as a convenience anywhere in this package.

## What was deliberately not built

- **No push exporter, no OpenTelemetry SDK, no HTTP client** (D1, D2). The seams are
  `MetricsSink` and `Tracer` and they are two methods each.
- **No wiring into `commerce-api`, `action-executor` or the frontends.** Other sessions own
  those files tonight. Everything above is the recipe.
- **No `metric_events` table.** Spec 25.5 lists one; it belongs to the Merchant Growth
  Engine's revenue projections (spec 9.3), which are business facts with a merchant
  audience. Operational telemetry is a different thing with a different audience and a
  different durability requirement, and putting them in one place would put a dashboard
  refresh on the same table as a money write.
- **No alerting rules, no dashboards.** They belong to the deployment (spec 23), and a
  dashboard checked into an application repository is a dashboard that drifts.
- **No sampling.** At this volume it costs nothing and it makes the numbers exact, and an
  exact number is worth more than a cheap one when there are three of you reading it.

## Testing

386 tests in `packages/platform-observability/tests`. The ones that matter:

- `test_po_metrics` — the exposition parses, `+Inf` equals `_count`, buckets are
  cumulative, label values escape, rendering is deterministic; two tenants stay separate
  and a tenant handle cannot address another tenant; cardinality caps; **a failing sink
  never raises into its caller** and is quarantined after five failures.
- `test_po_redaction` — every secret is passed *deliberately* and does not appear: a card
  number under an innocent field name, a bearer token in a message, a whole webhook body,
  a `Secret` in an f-string. Alongside the negatives that keep it useful: a UUID, an
  amount in minor units and a SHA-256 audit hash all survive.
- `test_po_correlation` — the id survives an `await`, several frames, and a spawned task;
  a task cannot corrupt its parent or its siblings; a thread does not inherit it.
- `test_po_logs` — one line per event even with a newline in a field; a field cannot
  overwrite the envelope; a record from a package that has never heard of this one still
  comes out redacted with the correlation id on it.
- `test_po_boundary` — reads the source with `ast` and fails on any import outside the
  standard library, any module that could reach the network or start a thread, and any
  recording method whose annotation stops saying `None`.
