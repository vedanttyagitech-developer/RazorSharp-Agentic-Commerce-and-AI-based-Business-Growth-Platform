"""The application factory.

``create_app`` assembles the whole service: settings, the merchant registry, the error
and idempotency handlers, and every router in
:data:`commerce_api.routers.ROUTERS`.

Two properties are worth stating because they are easy to lose:

**Import-safe.** Importing this module opens no connection, reads no environment variable
and starts no background task. ``create_app()`` with no argument reads the environment;
``create_app(settings)`` takes a configuration built from a dictionary. A test therefore
builds a fully working app without a ``.env`` anywhere near it, and an import in a
documentation tool cannot accidentally connect to a database.

**No unit edits this file.** Routers are included from a list that lives in
``routers/__init__.py``. That is what lets the catalogue, checkout, payment, evidence and
scenario units be built in parallel: each adds endpoints to its own module, and none of
them touches the application factory.

**Observability wraps everything and decides nothing.** The one middleware this service
installs binds a correlation scope, counts the request and times it; where it sits in the
stack, and why, is written out at :func:`create_app`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI

from .errors import install_error_handlers
from .idempotency import install_idempotency_handler
from .merchants import MerchantRegistry
from .observability import ObservabilityMiddleware, configure_process_logging
from .routers import ROUTERS
from .settings import Settings, get_settings

__all__ = ["API_VERSION", "create_app"]

logger: Final[logging.Logger] = logging.getLogger(__name__)

API_VERSION: Final[str] = "0.1.0"

_DESCRIPTION: Final[str] = """
Governed agentic commerce over the Transaction Assurance Kernel.

**Agents propose; deterministic systems authorize and execute.** Two consequences show up
directly in this contract:

* A refused money action is **HTTP 200 carrying a structured decision**, not a 4xx. The
  decision names a closed `RecoveryCode`, the exact deltas that caused it and the next
  version to approve. A denial is the system working (ADR 0003 D15).
* Errors -- as distinct from decisions -- are RFC 9457 problem details
  (`application/problem+json`).

Every mutation requires an `Idempotency-Key`. Re-sending one replays the original
response with `Idempotent-Replayed: true`; re-using one with a different body is refused
with 422 and executes nothing.

Amounts are integer minor units beside an ISO 4217 code. Timestamps are RFC 3339 UTC.
""".strip()


def _attach_specialist_runner(app: FastAPI, *, allow_ambient_env: bool) -> None:
    """Decide which runner answers a turn, and say so out loud either way.

    ``routers.agent._runner`` reads ``app.state.agent_runner`` and falls back to the
    deterministic runner when it is ``None``. Until this function existed the attribute was
    assigned nowhere outside a test, so every turn -- buyer and merchant, typed and spoken --
    took the fallback silently while the product called itself agentic. A missing feature
    announces itself; a silent fallback does not, which is why this logs whichever path it
    chooses.

    **The two halves it joins were built to different contracts.** ``AdkSpecialistRunner``
    implements the agent runtime's harness protocol, ``async __call__(bound, message, turn,
    session) -> SpecialistReply``, while ``TurnRunner`` in ``agent_service`` wants
    ``run(turn, chosen, tools) -> TurnOutcome`` synchronously. Different name, different
    arity, different types, different colour. Attaching the ADK runner directly is not a
    missing line but a live ``AttributeError`` on every turn: measured, not predicted, on
    ``gemini-3.8-flash`` with Vertex configured. ``services.agent_bridge.SpecialistBridge``
    is the adapter, and it lives in the service layer because it is the one object that must
    know both vocabularies.

    **Only the Shopping Specialist is model-backed, and the log line says so.** The bridge's
    own module docstring carries the reasons; the point here is that "which specialists a
    model answers" is a fact an operator reads out of the log rather than infers from a
    reply. Every other route keeps the deterministic runner, which is not a degraded mode of
    the same thing.

    Construction never fails startup. An unimportable runtime, an unconfigured Vertex or a
    runner that raises on the way up all end the same way: ``agent_runner`` stays ``None``,
    ``routers.agent`` reads ``None`` and the deterministic runner answers every turn.

    The imports stay inside the function because this module's contract is that importing it
    opens no connection and reads no environment, and ``google.adk`` reaches for credentials
    on the way in. ``agent_bridge`` itself imports nothing from ``google.*``; it takes the
    runtime as an argument, which is what keeps the seam testable without Vertex.

    ``allow_ambient_env`` is the same distinction :func:`create_app` already makes between
    its two documented construction modes, threaded one function further in. When a caller
    passed an explicit ``Settings`` -- the "construct from a dictionary in a test" path both
    that class's docstring and this module's promise to read no environment describe as
    self-contained -- ``vertex_configured()`` is never called at all, so a developer's own
    exported ``GOOGLE_GENAI_USE_VERTEXAI`` cannot attach a real Gemini bridge to a test that
    built its app from a dict specifically to avoid one. This was the second half of the
    ambient-environment leak that was traced to :class:`Settings`: the first half let a
    developer's shell fill in a ``Settings`` field a test meant to leave unset, and this half
    let it decide, independently of any ``Settings`` field, whether the SAME test's app was
    quietly agentic. Fixing only the first half left a test that constructs its OWN app
    directly from an explicit ``Settings`` -- rather than through the shared ``api_app``
    fixture, which already ``delenv``s this one variable -- exposed to exactly this.
    """
    if not allow_ambient_env:
        app.state.agent_runner = None
        app.state.reasoning_specialists = ()
        logger.info(
            "agent turns run the deterministic runner: this app was built from an explicit "
            "Settings, which reads no ambient environment by contract"
        )
        return
    # Two attributes, set on every path out of here, are what make the reasoning mode a
    # fact an operator reads rather than one they infer. ``agent_runner`` is the thing the
    # router actually uses; ``reasoning_specialists`` is the same fact in a shape a health
    # route can serve, so the answer to "is this process bridged" survives without importing
    # the bridge or re-running this decision. Deterministic-only is the empty tuple, not a
    # missing attribute, so a reader distinguishes "no specialist is model-backed" from
    # "the question was never answered".
    app.state.agent_runner = None
    app.state.reasoning_specialists = ()
    try:
        from agent_runtime.runtime_adk import DEFAULT_MODEL, vertex_configured
    except Exception as exc:  # noqa: BLE001 - an unimportable runtime is a fallback, not a stop
        # A degraded process that still answers is the trap this whole function exists to
        # avoid: it looks agentic and is not. Every fallback path is therefore a WARNING, so
        # the mode is visible in a log an operator skims rather than buried at INFO -- a
        # stale process once hid a working bridge for nine hours precisely because nothing
        # said which mode it was in. The reasons stay distinct because the remedies are:
        # an unimportable runtime is a deployment that shipped without the runtime package.
        logger.warning(
            "agent turns run the deterministic runner: the runtime will not import: %s", exc
        )
        return
    if not vertex_configured():
        # Distinct from the import failure above: the package is present, but the process
        # was started without the Vertex environment that ``AdkSpecialistRunner`` needs to
        # reach a model. Named env vars, never their values -- this repo redacts, and the
        # variable names are configuration shape, not a credential.
        logger.warning(
            "agent turns run the deterministic runner: Vertex is not configured "
            "(GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT)",
        )
        return
    try:
        from agent_runtime.runtime_adk import AdkSpecialistRunner

        from .services.agent_bridge import SpecialistBridge

        # One model, named once. ``AdkSpecialistRunner`` resolves it from the environment
        # (``AGENT_RUNTIME_MODEL``, else ``DEFAULT_MODEL``) and holds one session service for
        # the process; passing a second model here, or building a second runner beside it,
        # would give the same conversation two memories.
        bridge = SpecialistBridge(AdkSpecialistRunner())
    except Exception as exc:  # noqa: BLE001 - a runner that will not build is a fallback
        # The third distinct reason: Vertex was configured and the runtime imported, but the
        # runner raised on the way up (bad credentials, an unreachable project). The type is
        # named so the log distinguishes this from the two config answers above.
        logger.warning(
            "agent turns run the deterministic runner: the bridge would not build: %s: %s",
            type(exc).__name__,
            exc,
        )
        return
    app.state.agent_runner = bridge
    app.state.reasoning_specialists = tuple(
        sorted(specialist.value for specialist in bridge.bridged)
    )
    # The one healthy path, and the only INFO: a process that reached a model is not a
    # thing an operator has to hunt for in a warning stream.
    logger.info(
        "agent turns run %s for %s; every other specialist runs the deterministic runner",
        DEFAULT_MODEL,
        ", ".join(app.state.reasoning_specialists),
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001 - ASGI contract
    """Process-level setup, run once when a server starts serving this app.

    Logging configuration belongs here rather than at import: a module that reconfigures
    logging when it is imported reconfigures it for anything that imports it, including a
    documentation tool and a test suite that had its own opinion. :func:`configure_process_logging`
    declines when something else already owns the root logger, so building an app inside
    pytest leaves pytest's capture alone.
    """
    configure_process_logging()
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API.

    :param settings: an explicit configuration. Omit it to read the process environment
        through :func:`commerce_api.settings.get_settings`, which is what the server
        entry point does.

    The merchant registry is created per app rather than per process, so two apps in one
    test session hold separate simulated catalogues and one test's price injection cannot
    leak into another's assertions. In production there is one app and one process
    (ADR 0003 D14).
    """
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title="Governed Agentic Commerce",
        version=API_VERSION,
        description=_DESCRIPTION,
        # OpenAPI 3.1, so the schema speaks JSON Schema 2020-12 and nullable unions
        # generated by pydantic v2 survive into a generated client unchanged
        # (specification 24.1).
        openapi_version="3.1.0",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )

    app.state.settings = resolved
    app.state.merchants = MerchantRegistry()
    # Real boot (no explicit `settings`) reads the process environment throughout, Vertex
    # included -- that is what "read the process environment" in this function's own
    # docstring means. An explicit `Settings` is the other documented mode, and that mode's
    # whole point is a self-contained app: it must not pick up a developer's own exported
    # GOOGLE_GENAI_USE_VERTEXAI and quietly become agentic underneath a test that built its
    # app from a dict specifically to avoid exactly that.
    _attach_specialist_runner(app, allow_ambient_env=settings is None)

    # Observability is the outermost thing this file installs, and it is a different
    # layer from the two calls below rather than a competitor for the same slot. Starlette
    # builds ServerErrorMiddleware -> user middleware -> ExceptionMiddleware -> router, and
    # the handlers installed below are registrations *inside* those two framework
    # middlewares, not entries in the user stack. So this sits above every handled refusal
    # and below the last-resort 500 handler, which is the right place for both of its jobs:
    # the scope is bound before any router code runs, so every log line inside the request
    # carries the correlation id; and the response it measures is the one the client
    # actually receives, RFC 9457 problem details and kernel denials included. Putting it
    # under ExceptionMiddleware instead would mean timing only the requests that did not
    # refuse anything, which is the half of the traffic nobody needs to see.
    app.add_middleware(ObservabilityMiddleware)

    # Handlers before routers: an exception raised while including a router should still
    # be a problem detail if it somehow reaches a client.
    install_error_handlers(app)
    install_idempotency_handler(app)

    for router in ROUTERS:
        app.include_router(router)

    return app
