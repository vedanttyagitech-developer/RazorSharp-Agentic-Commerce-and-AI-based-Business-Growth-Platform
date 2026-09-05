"""Where this service becomes traceable: one correlation id, one JSON line, two numbers.

``packages/platform-observability`` was written complete and mounted nowhere. This module
is the API half of mounting it, and it is deliberately the *only* file in
``commerce_api`` that knows how that package works -- ``app.py`` adds a middleware and
calls a function, ``deps.py`` adopts an id and binds a scope, and neither of them imports
anything else from it.

Three decisions are made here rather than in the package, because each of them is a fact
about this application rather than about telemetry in general.

**The middleware mints the correlation id, and ``deps`` adopts it.** Before this existed,
``deps._correlation_id`` was the only thing that resolved ``X-Correlation-Id``, and it ran
part-way through the request -- after the bearer token had been read. A middleware that
minted its own would give a request *two* ids: one on the API's log lines and a different
one on the outbox row and the audit chain, which is precisely the join the whole exercise
exists to make. So resolution moved out here, to the first code that sees the request, and
the id is handed down through ``scope["state"]``. ``deps`` still resolves the header itself
when no middleware ran -- a test that builds a bare app, for instance -- so nothing depends
on this module being mounted.

**The route label is the template, resolved after dispatch.** ``scope["route"]`` is set by
the router while it matches, so it is read in the ``finally`` rather than up front. The
resolved path is never used: ``/v1/checkouts/6f2c.../approve`` would put a buyer's checkout
id into an exported time series that outlives the request by weeks (ADR 0007 D5).

**Logging is configured only when nobody else owns stderr.** ``configure_logging`` replaces
the root logger's handlers, which is exactly right for a process uvicorn started and
exactly wrong inside pytest, where the root handlers belong to the framework that is
capturing this test's output. The lifespan therefore installs the JSON formatter only when
the root logger is empty, which is the honest reading of "a library must not fight the
application over its own stderr".

Nothing here can change what the service answers. The middleware adds a response header and
records two instruments; it swallows no exception, rewrites no status, and holds no
transaction. A kernel denial is HTTP 200 with ``allowed: false`` before this file existed
and after it (ADR 0003 D15).
"""

from __future__ import annotations

import logging
import time
from typing import Final

from platform_observability import (
    PROMETHEUS_CONTENT_TYPE,
    MetricsRegistry,
    bind_scope,
    configure_logging,
    correlation_id_from_header,
    default_registry,
)
from platform_observability.correlation import CORRELATION_ID_HEADER
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "CORRELATION_ID_HEADER",
    "CORRELATION_STATE_KEY",
    "PROMETHEUS_CONTENT_TYPE",
    "TENANT_STATE_KEY",
    "UNAUTHENTICATED_TENANT",
    "ObservabilityMiddleware",
    "configure_process_logging",
    "registry",
]

#: The key on ``scope["state"]`` carrying the id the middleware resolved, read by
#: :func:`commerce_api.deps._correlation_id`. ``scope["state"]`` rather than a context
#: variable because it is one mutable dict shared by everything in the request, in both
#: directions; a context variable written downstream is written into a *copy* and never
#: reaches the middleware again.
CORRELATION_STATE_KEY: Final[str] = "correlation_id"

#: The key the session dependency writes the tenant to, on its way back out to the
#: middleware. Same reasoning, opposite direction.
TENANT_STATE_KEY: Final[str] = "tenant_id"

#: The tenant label for a request that never resolved one. A sentinel rather than no
#: series at all: an unauthenticated 401 and a 404 have no tenant, and dropping them would
#: hide exactly the traffic an incident starts with.
UNAUTHENTICATED_TENANT: Final[str] = "unauthenticated"

#: uvicorn attaches its own handlers to these and sets ``propagate = False``, so their
#: records never reach the root logger and never meet :class:`JsonFormatter`. That matters
#: beyond formatting: ``uvicorn.access`` writes the request line, query string included, so
#: leaving it alone would leave the one logger in the process most likely to print a
#: buyer's identifier as the only one redaction does not cover.
_UVICORN_LOGGERS: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")


def registry() -> MetricsRegistry:
    """The process-wide registry, with the platform catalogue already registered."""
    return default_registry()


def configure_process_logging(*, level: int = logging.INFO) -> bool:
    """Make every log line in this process one redacted JSON object. Returns whether it did.

    Declines when the root logger already has handlers, because that means something else
    -- pytest, or an operator's own configuration -- is already responsible for this
    process's output, and replacing it would be a library deciding it knows better.

    When it does configure, it also detaches uvicorn's own handlers and lets those loggers
    propagate, so the access log is redacted and correlated like everything else rather
    than being the one stream that is not.
    """
    root = logging.getLogger()
    if root.handlers:
        return False
    configure_logging(level=level)
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
    return True


class ObservabilityMiddleware:
    """Bind the correlation scope for the request, then count it and time it.

    Pure ASGI rather than ``BaseHTTPMiddleware``, and the difference is not stylistic.
    ``BaseHTTPMiddleware`` runs the downstream app in a separate anyio task, so a context
    variable bound here would be bound in the wrong task, and ``scope["route"]`` -- which
    the router writes into the scope during dispatch -- would have to be read back across
    that boundary. A pure ASGI middleware runs the app in the caller's own task and reads
    both directly.
    """

    __slots__ = ("app",)

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websockets. A websocket has no status and no single duration,
            # and the HTTP instruments would be a lie about both.
            await self.app(scope, receive, send)
            return

        correlation = correlation_id_from_header(Headers(scope=scope).get(CORRELATION_ID_HEADER))
        scope.setdefault("state", {})[CORRELATION_STATE_KEY] = correlation

        # 500 rather than 0: an exception that escapes upward is answered by Starlette's
        # ServerErrorMiddleware, which sits *above* this one, so `send_wrapper` never runs
        # for it. Defaulting to 500 counts that request as the failure it was instead of
        # inventing a status class nobody serves.
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = _with_correlation(message["headers"], correlation)
            await send(message)

        started = time.perf_counter()
        with bind_scope(correlation):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                # In `finally`, so a request that raised is still counted. The exception
                # is not caught and not touched: what the client is told is decided above
                # this middleware and below it, never here.
                self._record(scope, status, time.perf_counter() - started)

    def _record(self, scope: Scope, status: int, elapsed: float) -> None:
        """Both HTTP instruments, from what the scope knows once dispatch has happened."""
        route = getattr(scope.get("route"), "path", "unmatched")
        tenant = scope.get("state", {}).get(TENANT_STATE_KEY) or UNAUTHENTICATED_TENANT
        method = scope.get("method", "")
        metrics = registry().for_tenant(tenant)
        metrics.observe(
            "commerce_http_request_duration_seconds", elapsed, route=route, method=method
        )
        # The status *class*, not the status. Three of these are a dashboard; sixty of them
        # is a cardinality problem that answers no question the class does not.
        metrics.increment(
            "commerce_http_requests_total",
            route=route,
            method=method,
            status=f"{status // 100}xx",
        )


def _with_correlation(
    headers: list[tuple[bytes, bytes]], correlation: str
) -> list[tuple[bytes, bytes]]:
    """The response headers with ``X-Correlation-Id`` on them, as a new list.

    A new list rather than an ``append``: the list handed to ``http.response.start`` is a
    ``Response``'s own ``raw_headers``, and an idempotent replay that re-sends a stored
    response object would otherwise accumulate one header per send.

    An id the application set itself wins, because it knows something this middleware does
    not; nothing in this service does today, and if something starts to, silently
    overwriting it would be the wrong direction.
    """
    name = CORRELATION_ID_HEADER.lower().encode("latin-1")
    if any(key.lower() == name for key, _ in headers):
        return list(headers)
    return [*headers, (name, correlation.encode("latin-1"))]
