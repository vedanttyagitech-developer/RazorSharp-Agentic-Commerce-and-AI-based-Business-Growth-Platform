"""Liveness, and the redacted runtime facts a buyer surface needs before it starts.

Two endpoints, no authentication, and one rule that governs both: **nothing here is ever
a secret value.** ``GET /v1/config`` exists so the storefront can fetch the Razorpay key
id at runtime rather than baking it into a bundle (specification 11.1), and so a reviewer
can confirm from outside the process that it is in test mode. It reports the *prefix* of
the key id and never the id itself, a boolean for the webhook secret and never its value,
and no credential of any kind.

``/healthz`` deliberately touches no database. A liveness probe that fails when
PostgreSQL is slow gets the pod restarted mid-payment, which converts a recoverable
provider timeout into an unknown outcome with nobody left to reconcile it. Database
reachability is reported by ``/v1/config``, which is a diagnostic, not a probe.

``/v1/config`` also reports the reasoning **mode** -- whether the model-backed bridge is
attached and which specialists it answers -- for the same reason it reports the key
prefix: a process that fell back to the deterministic runner still answers every turn and
looks agentic, so the mode is a fact worth checking from outside rather than inferring
from a reply. It carries the mode and no more; the *reason* a process is deterministic
lives in the startup log, not here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from transaction_kernel import GuardedOperation, is_permitted

from ..deps import session_scope_for, settings_of
from ..settings import Settings

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    """Liveness only. Answered from process state, never from a dependency."""

    model_config = ConfigDict(extra="forbid")

    status: str


class DegradationOut(BaseModel):
    """One component that is not fully available, and what that means for the buyer."""

    model_config = ConfigDict(extra="forbid")

    component: str
    notice: str


class RazorpayFactsOut(BaseModel):
    """What the browser is allowed to know about this process's Razorpay credentials.

    ``key_id_prefix`` is ``rzp_test_`` or ``rzp_live_`` and nothing more. The full key id
    is public in Razorpay's own checkout flow, but it is not this endpoint's job to hand
    out credentials; the prefix answers the only question worth asking here, which is
    whether real money can move.
    """

    model_config = ConfigDict(extra="forbid")

    test_mode: bool
    key_id_prefix: str
    webhook_secret_configured: bool


class DatabaseFactsOut(BaseModel):
    """Whether this process can reach PostgreSQL, under each role it uses."""

    model_config = ConfigDict(extra="forbid")

    reachable: bool
    app_role: bool
    kernel_role: bool


class ReasoningFactsOut(BaseModel):
    """Which reasoning mode this process is in, and nothing about how it got there.

    A process that could not reach a model still answers every turn on the deterministic
    runner, so the product looks agentic whether or not it is -- which is how a stale
    process once hid a working bridge for nine hours. This reports the fact the log line
    also states, so it can be checked from outside without reading logs.

    ``bridged`` answers a narrower question than it sounds like it does, and that gap is
    the reason ``model_reached`` exists beside it. ``bridged`` is true the moment
    ``AdkSpecialistRunner`` is CONSTRUCTED -- three environment variables read and a
    session service allocated, no network call, no credential ever touched -- because
    that is all ``_attach_specialist_runner`` can check at import time. Application
    Default Credentials are resolved by the ADK SDK on the FIRST REAL REQUEST, deep in
    the model client, so a process with those three variables set but no ADC on the
    machine reports ``bridged: true`` while every turn quietly falls back -- identically
    to the process this field exists to tell apart from a healthy one.

    ``model_reached`` is therefore never inferred from configuration: it is ``null``
    until a bridged specialist's call to the model has actually returned at least once
    (null is "no turn has been asked yet", which is not the same claim as
    "unreachable"), then ``true`` or ``false`` for as long as that remains this
    process's most recent bridged answer. A missing ADC is an outage in the sense
    ``agent_service.py`` already gives that word -- the WARNING branch, not the ERROR
    one a genuine wiring defect raises -- and its fix is external to this process: the
    machine's own credential setup, not a code or config change here.

    ``specialists`` names which specialists the bridge answers, straight from its own
    ``bridged`` set. All three carry the *mode* and no more: not the model id, not the
    profile, not the reason a degraded process is degraded -- those are in the log,
    where an operator is, and none of them is a client's business.
    """

    model_config = ConfigDict(extra="forbid")

    bridged: bool
    specialists: list[str]
    model_reached: bool | None = None


class RuntimeConfigOut(BaseModel):
    """Redacted runtime facts (ADR 0003 endpoint catalogue, specification 21.4)."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    razorpay_mode: str
    razorpay: RazorpayFactsOut
    database: DatabaseFactsOut
    reasoning: ReasoningFactsOut
    #: The **platform-wide** Safe Mode switch, and only that one. This route is
    #: unauthenticated, so it has no tenant whose mode it could report -- and this service
    #: only ever declares the *tenant* scope (see ``routers/ops.py``: the platform-wide
    #: switch needs a transaction with no tenant bound and belongs to an operator with
    #: database access). So a tenant in Safe Mode is invisible here, by construction rather
    #: than by accident, and a reader who takes this field for "is the kill switch on for
    #: me" is reading it wrong. ``GET /v1/ops/safe-mode`` answers that, on a session.
    safe_mode: bool
    #: Which scope ``safe_mode`` describes. Always ``GLOBAL``; present so the field above
    #: cannot be mistaken for a tenant's answer by a client that never read this file.
    safe_mode_scope: str
    scenario_routes_enabled: bool
    demo_routes_enabled: bool
    degraded: list[DegradationOut]


@router.get("/healthz", response_model=HealthOut, summary="Liveness")
def healthz() -> HealthOut:
    """Is this process alive. Nothing more, on purpose -- see the module docstring."""
    return HealthOut(status="ok")


@router.get("/v1/config", response_model=RuntimeConfigOut, summary="Redacted runtime facts")
def runtime_config(request: Request) -> RuntimeConfigOut:
    """Everything a client may know about this process, and no credential.

    Never returns a key id, a key secret, a webhook secret, a scenario key or a
    connection URL. A test asserts that none of the configured secret values appears
    anywhere in this response.
    """
    settings = settings_of(request)
    razorpay = settings.razorpay()
    degraded: list[DegradationOut] = []

    # Read the reasoning mode off ``app.state`` rather than re-deriving it: the process made
    # this decision once at startup (``app._attach_specialist_runner``) and this endpoint
    # reports what it decided, not what it would decide now. Absent means an app was built
    # by a path that never ran the attach step, which is deterministic-only by construction
    # -- the same default the router falls back to -- so a missing attribute reads as "not
    # bridged", never as a 500.
    specialists = list(getattr(request.app.state, "reasoning_specialists", ()) or ())

    app_ok = _reachable(settings.database_url_app)
    kernel_ok = _reachable(settings.database_url_kernel)
    if not app_ok or not kernel_ok:
        degraded.append(
            DegradationOut(
                component="database",
                notice=(
                    "PostgreSQL is not reachable under every role this process needs. "
                    "Checkout, approval and payment are unavailable until it is."
                ),
            )
        )

    safe_mode, safe_mode_known = _safe_mode(settings)
    if not safe_mode_known:
        degraded.append(
            DegradationOut(
                component="safe_mode",
                notice=(
                    "The operating mode could not be read, so it is reported as NORMAL. "
                    "Treat that as unknown rather than as an assurance."
                ),
            )
        )

    return RuntimeConfigOut(
        profile=settings.profile.value,
        razorpay_mode="test" if razorpay.is_test_mode else "live",
        razorpay=RazorpayFactsOut(
            test_mode=razorpay.is_test_mode,
            # Everything before and including the mode marker: "rzp_test_" / "rzp_live_".
            key_id_prefix=razorpay.key_id[: razorpay.key_id.find("_", 4) + 1],
            webhook_secret_configured=bool(razorpay.webhook_secret),
        ),
        database=DatabaseFactsOut(
            reachable=app_ok and kernel_ok, app_role=app_ok, kernel_role=kernel_ok
        ),
        reasoning=ReasoningFactsOut(
            bridged=bool(specialists),
            specialists=specialists,
            # Read off the bridge object itself, which is where `run_turn` records it and
            # the only place the fact is durable across requests on this process. Absent
            # entirely -- no bridge attached, or a scripted double in a test -- reads as the
            # honest `None`: no turn has proven anything either way.
            model_reached=getattr(request.app.state.agent_runner, "model_reached", None),
        ),
        safe_mode=safe_mode,
        safe_mode_scope="GLOBAL",
        scenario_routes_enabled=settings.scenario_routes_enabled,
        demo_routes_enabled=settings.demo_routes_enabled,
        degraded=degraded,
    )


def _reachable(url: str) -> bool:
    """``SELECT 1`` under one role's credentials. Any failure is a False, never a 500.

    This endpoint's job is to *report* that the database is down, so it must survive the
    database being down.
    """
    try:
        with session_scope_for(url) as session:
            session.execute(text("SELECT 1"))
    # Broad by design: this endpoint reports that the database is down, so it must
    # survive every way it can be down.
    except Exception:
        return False
    return True


def _safe_mode(settings: Settings) -> tuple[bool, bool]:
    """The global Safe Mode state, and whether it could actually be read.

    Global, and *only* global: the tenant argument is ``None`` on purpose, because this
    route is unauthenticated and has no tenant to ask about. Since this service declares
    Safe Mode at tenant scope and nowhere else, a tenant that has been put into Safe Mode
    through ``POST /v1/ops/safe-mode`` does not appear here. The field carrying this
    answer says so.

    Asked as "may a delegated debit proceed" rather than by reading the mode row
    directly, because that is the question Safe Mode exists to answer and
    :func:`transaction_kernel.is_permitted` is the one component permitted to answer it.

    Returns ``(safe_mode_active, known)``. An unreadable mode is reported as *not* active
    with ``known=False`` and a degradation notice, rather than as active: claiming the
    kill switch is on when nobody can read it would stop a working demonstration on the
    strength of a failed query.
    """
    try:
        with session_scope_for(settings.database_url_app) as session:
            permitted, _ = is_permitted(session, None, GuardedOperation.DELEGATED_DEBIT)
    except Exception:  # Broad by design: see the docstring.
        return False, False
    return not permitted, True
