"""durable-worker: executes admitted commands and is the only process that calls Razorpay.

See docs/adr/0003-service-layer.md, decisions D1, D3, D5, D7, D8, D10 and D13.

The whole package exists to make one sentence true: **every provider mutation consumes
exactly one Execution Grant, consumed in a committed transaction before the network
call.** Everything else here is a consequence of it. The grant is consumed and committed
first, so a crash produces a redelivery that reconciles instead of a second charge. The
provider answer is classified as unknown unless something rules the mutation out, so a
lost response is never mistaken for a failure. Reconciliation is bounded and then handed
to a person, so nothing loops forever pretending to make progress.

The public surface is deliberately small: settings and a runtime, a loop, and an entry
point. The handlers are reachable through :mod:`durable_worker.handlers` for tests and
for anyone reading the ordering, and the transport through
:mod:`durable_worker.transport`.
"""

from .loop import TenantRef, TickReport, dispatch, list_tenants, run_forever, run_once
from .settings import (
    Profile,
    WorkerRuntime,
    WorkerSettings,
    build_runtime,
    get_settings,
)
from .transport import HttpxTransport

__all__ = [
    "HttpxTransport",
    "Profile",
    "TenantRef",
    "TickReport",
    "WorkerRuntime",
    "WorkerSettings",
    "build_runtime",
    "dispatch",
    "get_settings",
    "list_tenants",
    "run_forever",
    "run_once",
]
