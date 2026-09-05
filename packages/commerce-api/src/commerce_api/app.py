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
    """Give the five specialists a model, or say out loud why they have none.

    Until this existed, ``app.state.agent_runner`` was assigned nowhere outside a test, so
    ``routers.agent._runner`` read ``None`` on every request and every turn -- buyer and
    merchant, typed and spoken -- fell through to ``DeterministicRunner``. The platform
    answered from templates while calling itself agentic, and nothing said so. That silence
    is the real defect here: a missing feature announces itself, a silent fallback does not.
    So this logs either way, naming the model when there is one and the failure when there
    is not.

    **There is no separate switch.** The precondition for a model-backed turn is Vertex
    credentials, so that is what is checked -- through the agent runtime's own
    ``vertex_configured``, rather than a flag beside it that could disagree, and rather than
    a second copy of the same environment check here.

    It is checked rather than discovered, because ``AdkSpecialistRunner.__init__`` only
    stores its arguments: it succeeds without credentials and fails later, on the first
    turn. Attaching it unconditionally would therefore have replaced today's quiet template
    with a failure on every request, which is how a test suite discovered this branch.

    **The import is inside the function deliberately.** This module's stated contract is
    that importing it opens no connection and reads no environment, and ``google.adk`` is a
    heavy import that reaches for credentials on the way in. ``routers/agent.py`` avoids it
    for the same reason, which is why the runner is read off ``app.state`` rather than built
    where it is used.

    **Construction never fails startup.** Bad credentials, an unreachable project or a model
    that does not answer leave the deterministic path in place with the reason stated.
    Specification 30 requires a deterministic fallback when the model fails, and a process
    that exits instead has no fallback at all.

    There is deliberately **no second model**. If ``gemini-3.8-flash`` cannot be reached the
    answer is the deterministic runner, not a quieter model answering in its place: a demo
    that silently substitutes a model is claiming something it is not doing.
    """
    try:
        from agent_runtime.runtime_adk import (
            DEFAULT_MODEL,
            AdkSpecialistRunner,
            vertex_configured,
        )

        if not vertex_configured():
            app.state.agent_runner = None
            logger.info(
                "agent turns run the deterministic runner: Vertex is not configured "
                "(GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT)",
            )
            return
        runner = AdkSpecialistRunner()
    except Exception as exc:  # noqa: BLE001 - every failure here degrades; none stops the server
        app.state.agent_runner = None
        logger.warning(
            "agent turns run the deterministic runner: no model runtime (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    app.state.agent_runner = runner
    logger.info("agent turns run on %s", DEFAULT_MODEL)


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
