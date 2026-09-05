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
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import FastAPI

from .errors import install_error_handlers
from .idempotency import install_idempotency_handler
from .merchants import MerchantRegistry
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


def _attach_specialist_runner(app: FastAPI) -> None:
    """Decide which runner answers a turn, and say so out loud either way.

    ``routers.agent._runner`` reads ``app.state.agent_runner`` and falls back to the
    deterministic runner when it is ``None``. Until this function existed the attribute was
    assigned nowhere outside a test, so every turn -- buyer and merchant, typed and spoken --
    took the fallback silently while the product called itself agentic. A missing feature
    announces itself; a silent fallback does not, which is why this logs whichever path it
    chooses.

    **It currently always chooses the deterministic runner, and that is a statement about a
    seam rather than a missing line.** ``gemini-3.8-flash`` is declared, Vertex is reachable,
    and ``AdkSpecialistRunner`` builds -- but it implements the agent runtime's harness
    protocol, ``async __call__(bound, message, turn, session) -> SpecialistReply``, while
    ``TurnRunner`` in ``agent_service`` wants ``run(turn, chosen, tools) -> TurnOutcome``
    synchronously. Different name, different arity, different types, different colour.
    ``TurnRunner``'s own docstring claims the ADK adapter "satisfies this by running its
    LlmAgent inside ``run``"; it does not, and nothing ever forced the two to meet because
    no code path connected them.

    Attaching it regardless would be worse than leaving it off. Every turn would call the
    model, raise ``AttributeError``, and answer through the fallback -- so every reply would
    open with "the reasoning layer is unavailable" while a correct deterministic answer was
    available the whole time. That is measured rather than predicted: a live turn against
    ``gemini-3.8-flash`` with Vertex configured did exactly that.

    What the bridge needs, when it is built: the sync/async boundary, ``TurnInput`` to
    ``SpecialistInput``, this service's ``ToolExecutor`` to the factory's ``BoundToolset``,
    and ``SpecialistReply`` back to ``TurnOutcome``. Until then the five specialists are
    declared, routed and tooled, and they answer from the platform's own records.

    The import stays inside the function because this module's contract is that importing it
    opens no connection and reads no environment, and ``google.adk`` reaches for credentials
    on the way in.
    """
    app.state.agent_runner = None
    try:
        from agent_runtime.runtime_adk import DEFAULT_MODEL, vertex_configured
    except Exception as exc:  # noqa: BLE001 - an unimportable runtime is a fallback, not a stop
        logger.info("agent turns run the deterministic runner: %s", exc)
        return
    if not vertex_configured():
        logger.info(
            "agent turns run the deterministic runner: Vertex is not configured "
            "(GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT)",
        )
        return
    logger.info(
        "agent turns run the deterministic runner: %s is reachable, but no adapter presents "
        "the harness runner through TurnRunner",
        DEFAULT_MODEL,
    )


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
    )

    app.state.settings = resolved
    app.state.merchants = MerchantRegistry()
    _attach_specialist_runner(app)

    # Handlers before routers: an exception raised while including a router should still
    # be a problem detail if it somehow reaches a client.
    install_error_handlers(app)
    install_idempotency_handler(app)

    for router in ROUTERS:
        app.include_router(router)

    return app
