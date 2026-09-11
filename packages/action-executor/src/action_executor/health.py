"""A liveness endpoint that can actually fail.

``infra/kubernetes/base/workloads/action-executor.yaml`` names a
contract this module finally keeps: ``GET /healthz`` on ``WORKER_HEALTH_PORT`` answers 200
*while the outbox loop is alive*. Until now the probe pointed at nothing, and the manifest
carried a comment telling operators to patch it out.

WHY A HEARTBEAT RATHER THAN A BARE 200
--------------------------------------
The easy version binds a socket and always answers 200. That is worse than having no probe
at all, because Kubernetes would then report a wedged worker as healthy for ever, and the
one failure this process can suffer without dying is exactly the one it would hide: the
loop blocked on a provider call, a lock, or a connection pool with nothing left to give.
The process is still running; it has simply stopped doing the only thing it exists for.

So the loop stamps a monotonic timestamp after every tick and this server compares it to
now. No stamp inside ``stale_after`` seconds and the answer is 503 with the age in it, so
the reason is in the response rather than inferred from a restart.

CHOOSING ``stale_after``
------------------------
It has to exceed the worst honest tick, not the typical one. A batch is ``WORKER_BATCH_SIZE``
commands run sequentially (10 by default), each able to spend the provider timeout (20s), so
roughly 200s of legitimate work can pass between stamps. The default here is 300s, and the
manifest's ``failureThreshold: 5`` at ``periodSeconds: 30`` adds another 150s before a
restart -- deliberately slow, because restarting a worker mid-command costs one of the
command's eight attempts.

There is no readiness probe and this module offers none: the executor receives no traffic
(spec 23.1), so "ready" would mean nothing.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

__all__ = ["Heartbeat", "health_port", "serve_health", "stale_after_seconds"]

_LOG: Final = logging.getLogger("action_executor.health")

PORT_ENV: Final[str] = "WORKER_HEALTH_PORT"
DEFAULT_PORT: Final[int] = 8001
STALE_ENV: Final[str] = "WORKER_HEALTH_STALE_SECONDS"
DEFAULT_STALE_SECONDS: Final[float] = 300.0


class Heartbeat:
    """The last time the loop finished a tick, on the monotonic clock.

    Monotonic on purpose: a wall clock that steps backwards during an NTP correction would
    make a healthy worker look stale, and this value is only ever compared with itself.

    A lock rather than relying on the GIL for a float write: the intent is that a reader in
    the server thread never observes a torn or reordered value, and stating that in code is
    cheaper than arguing about which CPython versions make it safe implicitly.
    """

    __slots__ = ("_at", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = time.monotonic()

    def beat(self) -> None:
        """Record that a tick completed. Cheap enough to call every tick."""
        with self._lock:
            self._at = time.monotonic()

    def age_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._at


def health_port() -> int:
    raw = os.environ.get(PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        _LOG.warning("%s=%r is not an integer; using %d", PORT_ENV, raw, DEFAULT_PORT)
        return DEFAULT_PORT
    if not 1 <= port <= 65535:
        _LOG.warning("%s=%d is out of range; using %d", PORT_ENV, port, DEFAULT_PORT)
        return DEFAULT_PORT
    return port


def stale_after_seconds() -> float:
    raw = os.environ.get(STALE_ENV, "").strip()
    if not raw:
        return DEFAULT_STALE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning("%s=%r is not a number; using %s", STALE_ENV, raw, DEFAULT_STALE_SECONDS)
        return DEFAULT_STALE_SECONDS
    return value if value > 0 else DEFAULT_STALE_SECONDS


def _handler(heartbeat: Heartbeat, stale_after: float) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "action-executor"
        sys_version = ""  # do not advertise the Python version

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.path.split("?", 1)[0] != "/healthz":
                self._respond(HTTPStatus.NOT_FOUND, b'{"status":"not_found"}')
                return
            age = heartbeat.age_seconds()
            alive = age <= stale_after
            status = HTTPStatus.OK if alive else HTTPStatus.SERVICE_UNAVAILABLE
            body = (
                b'{"status":"ok","last_tick_seconds_ago":%.1f}' % age
                if alive
                else b'{"status":"stale","last_tick_seconds_ago":%.1f}' % age
            )
            self._respond(status, body)

        def _respond(self, status: HTTPStatus, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            """Silence the default stderr access log.

            Every probe would otherwise write a line, twice a minute for ever, into a
            stream this process uses for structured JSON. A failing probe is visible in
            Kubernetes events; it does not need to be visible here too.
            """

    return Handler


def serve_health(heartbeat: Heartbeat) -> Callable[[], None]:
    """Start the endpoint on a daemon thread; return a callable that stops it.

    Binding failures are logged and swallowed. A worker that refuses to process commands
    because it could not open a health socket has turned an observability problem into an
    outage, which is the wrong trade for this process.
    """
    port = health_port()
    stale_after = stale_after_seconds()
    try:
        # 0.0.0.0: a Kubernetes probe arrives from the node, not from inside the Pod.
        server = ThreadingHTTPServer(("0.0.0.0", port), _handler(heartbeat, stale_after))  # noqa: S104
    except OSError as exc:
        _LOG.warning("health endpoint not started on port %d: %s", port, exc)
        return lambda: None

    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="worker-health", daemon=True)
    thread.start()
    _LOG.info("health endpoint listening on :%d (stale after %.0fs)", port, stale_after)

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return stop
