"""Operator views: Safe Mode and the outbox.

Safe Mode is the kill switch (specification 10.3.2): it stops new delegated payments
without disabling reconciliation, refunds, support or fresh human-present checkout. The
outbox view shows leased, failed and revivable work.

**Owned by build unit F.**

Why the banner names both halves
--------------------------------
A kill switch that stops everything is not a kill switch, it is an outage, and an outage
harms the buyer it was meant to protect. So the response never says only "Safe Mode is
on": it carries the blocked list *and* the still-available list, straight from
``transaction_kernel.safe_mode``'s own classification, plus a live answer for the four
operations an operator actually asks about. A surface that had to compose that sentence
itself would eventually get it wrong in the direction of "everything is down".

Like the scenario controller, every route here is behind ``X-Scenario-Key`` and absent
from the production profile, and every route also requires a session -- the key says the
caller may operate the apparatus, the session says on whose tenant. Safe Mode in this
service is therefore always the **tenant** scope; the platform-wide switch requires a
transaction with no tenant bound (``safe_mode._bind_scope_for_write``) and belongs to an
operator tool, not to an HTTP request that authenticated as a tenant.

``GET /v1/ops/metrics`` is the exception to the second half of that: it takes the scenario
key and no session, because a metrics exposition is process-wide by nature and asking it
to pick a tenant would make it useless. It is here, on the operator surface, rather than at
the root, for the reason in :mod:`commerce_api.observability`: the exposition carries no
buyer data by construction, but it does describe the platform's shape and has no business
on the buyer-facing route table.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Final

import durable_work as dw
import transaction_kernel as tk
from commerce_domain import ActorType
from fastapi import APIRouter, Depends, Query, Response
from platform_db import OutboxEvent
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from transaction_kernel import GuardedOperation
from transaction_kernel.safe_mode import (
    SAFE_MODE_BLOCKED,
    SAFE_MODE_PERMITTED,
    ModeChangeReason,
    ModeResolution,
    OperatingModeName,
    banner,
    resolve_mode,
)

from ..deps import AppSession, KernelSession, SessionContext, require_scenario_key
from ..errors import ProblemError
from ..observability import PROMETHEUS_CONTENT_TYPE, registry
from ..schemas import rfc3339, uuid_str

router = APIRouter(
    prefix="/v1/ops",
    tags=["ops"],
    dependencies=[Depends(require_scenario_key)],
)

__all__ = ["router"]

#: The four questions an operator asks when the banner is up, answered live rather than
#: inferred from the two lists. ``is_permitted`` re-reads the mode inside the request's
#: transaction, so this cannot disagree with what admission will decide a moment later.
_REPORTED_OPERATIONS: Final[tuple[GuardedOperation, ...]] = (
    GuardedOperation.DELEGATED_DEBIT,
    GuardedOperation.HUMAN_PRESENT_CHECKOUT,
    GuardedOperation.REFUND_EXECUTE,
    GuardedOperation.RECONCILIATION,
)

#: Default reasons, so an operator throwing the switch in a hurry still produces an
#: attributable record. Both are in the kernel's closed vocabularies.
DEFAULT_ENTRY_REASON: Final[ModeChangeReason] = ModeChangeReason.OPERATOR_DECLARED_INCIDENT
DEFAULT_EXIT_REASON: Final[ModeChangeReason] = ModeChangeReason.INCIDENT_RESOLVED

#: How many outbox rows one page of the operator view returns.
DEFAULT_OUTBOX_LIMIT: Final[int] = 50
MAX_OUTBOX_LIMIT: Final[int] = 200

#: The two statuses a command sits in while it is still owed a delivery.
#:
#: ``LEASED`` is in flight and has its own deadline; ``DONE`` is finished. ``DEAD`` is
#: excluded on purpose: it already has its own count and its own revive control on the
#: operations tab, so folding it in here would report the one stuck command an operator
#: can already see, twice.
AWAITING_DELIVERY: Final[tuple[str, ...]] = (
    dw.OutboxStatus.PENDING.value,
    dw.OutboxStatus.FAILED.value,
)

#: Past this far out, a command's ``available_at`` was not set by a retry.
#:
#: Every delay this platform schedules is capped at the outbox policy's ceiling --
#: :func:`durable_work.backoff_seconds` clamps to ``backoff_cap_seconds``, and the
#: reconciliation handler's own backoff clamps to the same number -- so no failure path
#: can place a command beyond this line. A row that sits past it was put there by
#: something other than the retry machinery, and that is a fact about the tenant's money
#: which no count of statuses can state. Derived from the policy rather than written out,
#: because a number copied here would be a second opinion about the first one.
PARKED_BEYOND_SECONDS: Final[int] = dw.DEFAULT_POLICY.backoff_cap_seconds

#: How long past due a command may sit before the wait is the worker's fault, not the
#: schedule's.
#:
#: One whole lease. A worker polls far more often than it leases, so a command still
#: unclaimed a lease after its moment came is not a second away from being picked up --
#: nobody is picking it up. Taken from the same policy for the same reason as above.
OVERDUE_BEYOND_SECONDS: Final[int] = dw.DEFAULT_POLICY.lease_seconds


# ------------------------------------------------------------------------- wire shapes


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BannerOut(_Body):
    """The visible banner specification 10.3.2 requires, as structured fields.

    Deliberately not a sentence: the surface renders these into the operator's language
    and no model composes them.
    """

    scope: str
    tenant_id: str | None
    reason_code: str
    actor: str
    since: str
    blocked: list[str]
    still_available: list[str]


class SafeModeOut(_Body):
    """The effective mode for this tenant, with the evidence for it.

    ``blocked`` and ``still_available`` are present under ``NORMAL`` too, because the
    question "what would Safe Mode stop" is one an operator asks *before* throwing the
    switch, not only after.
    """

    mode: str
    scope: str
    tenant_id: str
    safe_mode: bool
    reason_code: str | None
    actor: str | None
    since: str | None
    banner: BannerOut | None
    blocked: list[str]
    still_available: list[str]
    permitted: dict[str, bool]
    revoked_grant_ids: list[str]


class SafeModeRequest(_Body):
    """Throw or stand down the kill switch for this tenant.

    ``reason`` is a closed vocabulary from the kernel, not free text: an incident record
    that says "because" is not the audited administrative action the specification asks
    for. Omitting it takes the operator-declared default.
    """

    enabled: bool
    reason: ModeChangeReason | None = None


class OutboxCommandOut(_Body):
    """One row of ``outbox_events`` as an operator needs to read it."""

    command_id: str
    command_type: str
    status: str
    attempts: int
    available_at: str
    leased_until: str | None
    correlation_id: str
    created_at: str


class OutboxWaitingOut(_Body):
    """Commands still owed a delivery that are not about to get one.

    A count of statuses cannot say this, and that is the whole reason this block exists.
    ``PENDING`` covers both a command the worker will lease in the next second and a
    command whose ``available_at`` is a week away, and on a summary screen those two read
    as the same reassuring number -- which is how a tenant with a refund parked past the
    weekend renders as settled.

    ``parked`` is scheduled further out than any retry could have put it and ``overdue``
    is past due and still unclaimed. They are separate because the remedies are opposite:
    a parked command is a scheduling decision somebody made, an overdue one is a worker
    that is not running. Both are counted across the whole tenant rather than off the
    page, for the same reason ``counts`` is, and both thresholds are reported in the
    response so a surface can say what line it is drawing instead of asserting a verdict
    the reader cannot check.

    ``oldest`` is the longest-waiting of the two sets by creation, or ``None`` when both
    are empty -- so a surface can name a command rather than print a bare number and
    leave an operator to go and find which one it meant.
    """

    parked: int
    overdue: int
    parked_beyond_seconds: int
    overdue_beyond_seconds: int
    oldest: OutboxCommandOut | None


class OutboxOut(_Body):
    """A page of commands and, separately, the counts across the whole tenant.

    The counts are not derived from the page. A rising ``FAILED`` count is a provider
    incident and a rising ``PENDING`` count is a worker shortage; those have opposite
    remedies, and reading either off a truncated page would point an operator the wrong
    way.
    """

    commands: list[OutboxCommandOut]
    counts: dict[str, int]
    waiting: OutboxWaitingOut
    limit: int


class ReviveOut(_Body):
    """What reviving one dead letter did."""

    command_id: str
    code: str
    status: str
    retry_at: str | None


# --------------------------------------------------------------------------- safe mode


def _view(
    session: Session,
    tenant_id: uuid.UUID,
    resolution: ModeResolution,
    *,
    revoked: tuple[uuid.UUID, ...] = (),
) -> SafeModeOut:
    """Render one resolution, asking the kernel about each reported operation.

    Both lists are sorted so a diff between two calls is a real change rather than set
    iteration order.
    """
    card = banner(resolution)
    return SafeModeOut(
        mode=resolution.mode.value,
        scope=resolution.scope.value,
        tenant_id=str(tenant_id),
        safe_mode=resolution.safe_mode,
        reason_code=resolution.reason_code,
        actor=resolution.actor,
        since=None if resolution.since is None else rfc3339(resolution.since),
        banner=None
        if card is None
        else BannerOut(
            scope=card.scope.value,
            tenant_id=uuid_str(card.tenant_id),
            reason_code=card.reason_code,
            actor=card.actor,
            since=rfc3339(card.since),
            blocked=[operation.value for operation in card.blocked],
            still_available=[operation.value for operation in card.still_available],
        ),
        blocked=sorted(operation.value for operation in SAFE_MODE_BLOCKED),
        still_available=sorted(operation.value for operation in SAFE_MODE_PERMITTED),
        permitted={
            operation.value: tk.is_permitted(session, tenant_id, operation)[0]
            for operation in _REPORTED_OPERATIONS
        },
        revoked_grant_ids=[str(value) for value in revoked],
    )


@router.get("/safe-mode", response_model=SafeModeOut, summary="Read the kill switch")
def read_safe_mode(ctx: SessionContext, session: AppSession) -> SafeModeOut:
    """The effective mode for this tenant, and what it does and does not stop.

    A read, so it runs as the app role. The mode is re-read from PostgreSQL inside this
    transaction rather than cached, which is the same guarantee admission gets: a switch
    thrown a second ago is visible now, not after a refresh interval.
    """
    return _view(session, ctx.tenant_id, resolve_mode(session, ctx.tenant_id))


@router.post("/safe-mode", response_model=SafeModeOut, summary="Throw or stand down the switch")
def set_safe_mode(
    body: SafeModeRequest, ctx: SessionContext, session: KernelSession
) -> SafeModeOut:
    """Enter or leave Safe Mode for this tenant. An authenticated, audited action.

    **An AGENT is refused, 403.** Specification 10.3.2 reserves the switch for an
    operator or an allowlisted deterministic incident rule, and says in terms that the
    LLM cannot enter or leave Safe Mode. The kernel refuses an ``AGENT`` actor too
    (``safe_mode._validate_actor``), but it does so with a ``ValueError``, which would
    surface as a 500 -- an agent hitting a control it may not touch is a permission
    answer, not a fault, and it should read like one.

    The actor recorded is ``OPERATOR``: the credential that reached this route is the
    scenario key, which is an operator credential. The session identity is carried inside
    the actor string so the record still says which console did it.

    Entering also withdraws this tenant's unused delegated grants. Refund grants are
    never swept -- a refund already admitted still completes, because a switch thrown to
    protect a buyer must not cancel money that buyer is already owed.
    """
    if ctx.actor_type is ActorType.AGENT:
        raise ProblemError(
            403,
            "Safe Mode is an operator control",
            "An agent may not enter or leave Safe Mode. Specification 10.3.2 reserves "
            "the switch for an operator or an allowlisted deterministic incident rule.",
            actor_type=ctx.actor_type.value,
        )

    actor = f"operator:session:{ctx.session_id}"
    revoked: tuple[uuid.UUID, ...] = ()
    if body.enabled:
        activation = tk.enter_safe_mode(
            session,
            tenant=ctx.tenant_id,
            reason=body.reason or DEFAULT_ENTRY_REASON,
            actor=actor,
            actor_type=ActorType.OPERATOR,
        )
        revoked = activation.revoked_grant_ids
    else:
        tk.leave_safe_mode(
            session,
            tenant=ctx.tenant_id,
            reason=body.reason or DEFAULT_EXIT_REASON,
            actor=actor,
            actor_type=ActorType.OPERATOR,
        )

    resolution = resolve_mode(session, ctx.tenant_id)
    if body.enabled and resolution.mode is not OperatingModeName.SAFE_MODE:  # pragma: no cover
        # Unreachable: the record was written in this transaction and resolve_mode reads
        # it back. Asserted rather than assumed, because reporting NORMAL after a
        # successful activation would tell an operator the switch failed to throw.
        raise ProblemError(
            500,
            "Safe Mode did not take effect",
            "The mode record was written but the scope still resolves to NORMAL.",
        )
    return _view(session, ctx.tenant_id, resolution, revoked=revoked)


# ------------------------------------------------------------------------------ outbox


@router.get("/outbox", response_model=OutboxOut, summary="Pending, failed and dead commands")
def list_outbox(
    ctx: SessionContext,
    session: AppSession,
    status: Annotated[dw.OutboxStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_OUTBOX_LIMIT)] = DEFAULT_OUTBOX_LIMIT,
) -> OutboxOut:
    """The operator's view of durable work in this tenant.

    Ordered newest first, because the rows an operator is looking for during an incident
    are the ones that just failed. Row-level security scopes the query to the session's
    tenant; the explicit predicate is written out as well, so the intent is readable at
    the call site.
    """
    query = select(OutboxEvent).where(OutboxEvent.tenant_id == ctx.tenant_id)
    if status is not None:
        query = query.where(OutboxEvent.status == status.value)
    rows = (
        session.execute(query.order_by(OutboxEvent.created_at.desc()).limit(limit)).scalars().all()
    )
    counted: dict[str, int] = {
        str(row.status): int(row.total)
        for row in session.execute(
            select(OutboxEvent.status, func.count().label("total"))
            .where(OutboxEvent.tenant_id == ctx.tenant_id)
            .group_by(OutboxEvent.status)
        ).all()
    }
    return OutboxOut(
        commands=[_command(row) for row in rows],
        # Every status present, so a zero reads as "none" rather than as "not measured".
        counts={member.value: counted.get(member.value, 0) for member in dw.OutboxStatus},
        waiting=_waiting(session, ctx.tenant_id),
        limit=limit,
    )


def _waiting(session: Session, tenant_id: uuid.UUID) -> OutboxWaitingOut:
    """Count the commands that are still owed a delivery and are not about to get one.

    Deliberately ignores the caller's ``status`` filter and the page limit. An operator
    who has filtered down to ``DONE`` has not thereby stopped a refund being parked, and
    a summary that quietly narrowed with the filter would go quiet at exactly the moment
    somebody was looking somewhere else.

    Both boundaries are computed by PostgreSQL's clock in the same statement that reads
    the rows, never from this process's ``datetime.now()``. The worker's readiness
    predicate is decided by the database clock, so a "due" this service worked out from
    its own clock would be answering a slightly different question than the one the
    worker acts on -- and the gap would show up as a phantom overdue count on a machine
    whose time had drifted.
    """
    scope = (OutboxEvent.tenant_id == tenant_id) & OutboxEvent.status.in_(AWAITING_DELIVERY)
    parked = OutboxEvent.available_at > func.now() + timedelta(seconds=PARKED_BEYOND_SECONDS)
    overdue = OutboxEvent.available_at <= func.now() - timedelta(seconds=OVERDUE_BEYOND_SECONDS)

    tallied = session.execute(
        select(
            func.count().filter(scope & parked).label("parked"),
            func.count().filter(scope & overdue).label("overdue"),
        ).select_from(OutboxEvent)
    ).one()
    # Oldest by creation rather than by `available_at`: the question a merchant is asking
    # is "how long has this been sitting there", and ordering by the due moment would
    # answer "which is furthest away", which puts the most recently parked command first.
    oldest = (
        session.execute(
            select(OutboxEvent)
            .where(scope & (parked | overdue))
            .order_by(OutboxEvent.created_at)
            .limit(1)
        )
        .scalars()
        .first()
    )
    return OutboxWaitingOut(
        parked=int(tallied.parked),
        overdue=int(tallied.overdue),
        parked_beyond_seconds=PARKED_BEYOND_SECONDS,
        overdue_beyond_seconds=OVERDUE_BEYOND_SECONDS,
        oldest=None if oldest is None else _command(oldest),
    )


def _command(row: OutboxEvent) -> OutboxCommandOut:
    return OutboxCommandOut(
        command_id=str(row.id),
        command_type=row.command_type,
        status=row.status,
        attempts=row.attempts,
        available_at=rfc3339(row.available_at),
        leased_until=_maybe(row.leased_until),
        correlation_id=str(row.correlation_id),
        created_at=rfc3339(row.created_at),
    )


def _maybe(moment: datetime | None) -> str | None:
    return None if moment is None else rfc3339(moment)


@router.post(
    "/outbox/{command_id}/revive",
    response_model=ReviveOut,
    summary="Return one dead letter to the queue",
)
def revive_command(command_id: uuid.UUID, ctx: SessionContext, session: KernelSession) -> ReviveOut:
    """Re-queue a ``DEAD`` command with a fresh attempt budget.

    The payload is untouched: what runs is the command that was originally committed, not
    an operator's reconstruction of it. Only a ``DEAD`` row is revived -- reviving a
    ``DONE`` one would re-run a completed money operation and reviving a ``LEASED`` one
    would race the worker holding it -- so anything else comes back as
    ``CONCURRENT_OPERATION`` with the status actually found. That is a 200 carrying the
    outcome rather than an error status: the operator asked a question and got a truthful
    answer.

    An agent is refused before anything is touched, exactly as it is at the Safe Mode
    switch. A ``DEAD`` row is almost always a ``PAYMENT_CREATE_ORDER`` or
    ``REFUND_EXECUTE`` command, so reviving one re-drives a money operation under its
    existing grant -- and re-driving money work is an operator action, never a delegable
    one. The scenario key gates this whole router, but the key is the operator apparatus,
    not an identity: without this check an ``AGENT`` session presenting the key reached a
    money control the Safe Mode switch already reserves for an operator (specification
    10.3.2).

    Runs as the kernel role because it updates ``outbox_events``. A command not visible
    to this tenant raises out of :func:`durable_work.revive` as a usage error rather than
    silently reporting nothing to revive.
    """
    if ctx.actor_type is ActorType.AGENT:
        raise ProblemError(
            403,
            "Reviving a command is an operator control",
            "An agent may not return a dead-lettered command to the queue: revive "
            "re-drives the money operation the command carries, which is an operator "
            "action and not one delegable to the party that proposed the purchase.",
            actor_type=ctx.actor_type.value,
        )
    outcome = dw.revive(session, command_id)
    return ReviveOut(
        command_id=str(command_id),
        code=outcome.code.value,
        status=outcome.status.value,
        retry_at=_maybe(outcome.retry_at),
    )


@router.get(
    "/metrics",
    summary="Prometheus exposition of this process's instruments",
    response_class=Response,
    responses={200: {"content": {PROMETHEUS_CONTENT_TYPE: {}}}},
)
def metrics() -> Response:
    """Render every registered instrument, in Prometheus 0.0.4 text format.

    Rendering is all this does. ``platform_observability`` has no HTTP client, no push and
    no background thread, so a metrics backend that is down is a scrape that fails and
    nothing else -- there is no socket on the path of a payment and no queue to fill. This
    endpoint is the whole of the transport, and it is owned here rather than by that
    package precisely so the package can stay that way (ADR 0007 D2).

    Every registered instrument is emitted with its ``# HELP`` and ``# TYPE`` even at zero
    series, which is what makes "nothing has happened yet" and "this was never wired up"
    two different-looking answers on the first day rather than during the first incident.

    ``WEB_CONCURRENCY`` is 1 (ADR 0003 D14), so this process's registry is the whole
    story. A second replica would need a scrape per replica and aggregation in Prometheus;
    it would not need a shared registry, and nothing here assumes one exists.
    """
    return Response(registry().render(), media_type=PROMETHEUS_CONTENT_TYPE)
