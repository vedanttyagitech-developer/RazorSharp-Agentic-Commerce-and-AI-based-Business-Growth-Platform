"""Lease, dispatch, report. The shape of one tick, and why it is three transactions.

    for each tenant:
        WORKER transaction: set_tenant, lease a batch          -- COMMIT immediately
        for each leased command: run its handler               -- its own transactions
        WORKER transaction: complete or fail that command      -- COMMIT

**Leasing commits before any work starts.** ``durable_work.lease`` locks its candidate
rows ``FOR UPDATE SKIP LOCKED``, and those locks live as long as the transaction does. A
worker that held the lease transaction open across a provider call would hold row locks
for the length of a network round trip, and every other worker would step over rows it
could have been running. Committing first also means that a crash mid-handler leaves the
row ``LEASED`` with a deadline the database will let lapse -- redelivery, which is the
outcome this system is built to survive.

**Tenants are enumerated, not inferred.** Outbox rows are row-level-security scoped, so a
worker with no tenant bound sees nothing at all -- the fail-closed direction, and a
silent one. The loop therefore reads ``tenants`` (the one table with no tenant column and
so no policy) and binds each tenant explicitly before leasing.

**Dispatch is by type, and every type is handled.** ``parse_leased_command`` refuses a
payload it does not recognise rather than returning ``None``, because a worker that
quietly completed an unrecognised command would lose it: for an outbox the failure
direction is "repeat, never skip".

**A dead letter is evidence.** A command that exhausts its attempts is buried by the
outbox and audited here in the same transaction, so "the platform stopped trying" is a
recorded fact with a correlation id rather than an absence somebody has to notice.

**Every command runs inside its own correlation scope, and that is the process boundary
being crossed.** The API admitted a money action, minted or adopted a correlation id, and
wrote it onto the outbox row in the same transaction as the state change. This loop binds
that id back around the handler, so the worker's log lines, its metrics and the provider
call underneath them all report the id the API reported -- and a payment becomes one thing
you can follow from an HTTP request through a database row into a Razorpay call, rather
than three unrelated events that happened to be about the same money. A thread does not
inherit a context variable, which is exactly right here: nothing should ever silently
inherit an id it did not get from the row it is working on.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

import transaction_kernel as tk
from commerce_domain import ActorType, RecoveryCode
from durable_work import (
    ApplyWebhookEventCommand,
    CreateOrderCommand,
    DeadLetter,
    LeasedCommand,
    OutboxUsageError,
    ReconcilePaymentCommand,
    ReconcileRefundCommand,
    RefundExecuteCommand,
    complete,
    fail,
    lease,
    parse_leased_command,
)
from durable_work.commands import ReserveDebitCommand
from platform_db import set_tenant
from platform_observability import (
    WORKER_COMMAND_TIMING,
    bind_scope,
    default_registry,
    timed,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from .handlers import HandlerError, HandlerResult, reason_key
from .handlers.apply_webhook import handle_apply_webhook
from .handlers.create_order import handle_create_order
from .handlers.housekeeping import HousekeepingReport, run_housekeeping
from .handlers.reconcile import handle_reconcile_payment, handle_reconcile_refund
from .handlers.refund import handle_refund_execute
from .handlers.reserve import handle_reserve
from .settings import WorkerRuntime

__all__ = [
    "TenantRef",
    "TickReport",
    "dispatch",
    "list_tenants",
    "run_forever",
    "run_once",
]

_LOG: Final = logging.getLogger("action_executor.loop")

#: The stream a buried command is recorded on. Its own aggregate rather than the
#: checkout's: a dead letter is a fact about the work item, and several command types that
#: never name a checkout can end up here.
_DEAD_LETTER_AGGREGATE: Final = "outbox_command"

_SELECT_TENANTS: Final = text("SELECT id, slug FROM tenants ORDER BY created_at")


@dataclass(frozen=True, slots=True)
class TenantRef:
    """One tenant the worker sweeps. The slug is for logs, never for authority."""

    tenant_id: uuid.UUID
    slug: str


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one pass over every tenant did. Returned so a test can assert a whole tick."""

    leased: int = 0
    completed: int = 0
    failed: int = 0
    dead_letters: int = 0
    housekeeping: HousekeepingReport | None = None
    details: tuple[str, ...] = field(default_factory=tuple)

    def __add__(self, other: TickReport) -> TickReport:
        return TickReport(
            leased=self.leased + other.leased,
            completed=self.completed + other.completed,
            failed=self.failed + other.failed,
            dead_letters=self.dead_letters + other.dead_letters,
            housekeeping=other.housekeeping or self.housekeeping,
            details=self.details + other.details,
        )


def list_tenants(runtime: WorkerRuntime) -> tuple[TenantRef, ...]:
    """Every tenant, read on the worker role.

    ``tenants`` is the one table with no ``tenant_id`` column and therefore no row-level
    security policy, which is exactly why this read is possible before a tenant is bound
    and why nothing else in a tick is.
    """
    with runtime.worker_session() as session:
        rows = session.execute(_SELECT_TENANTS).all()
    return tuple(TenantRef(tenant_id=row.id, slug=row.slug) for row in rows)


def run_once(
    runtime: WorkerRuntime,
    *,
    tenants: tuple[TenantRef, ...] | None = None,
    housekeeping: bool = False,
) -> TickReport:
    """One pass: lease and run every ready command for every tenant.

    ``tenants`` is injected by the caller that already has the list (and by tests);
    otherwise it is read fresh, so a tenant onboarded while the worker runs is picked up
    without a restart.
    """
    report = TickReport()
    for tenant in tenants if tenants is not None else list_tenants(runtime):
        report = report + _run_tenant(runtime, tenant, housekeeping=housekeeping)
    return report


def run_forever(
    runtime: WorkerRuntime,
    *,
    should_stop: Callable[[], bool] | None = None,
    max_ticks: int | None = None,
) -> TickReport:
    """Tick until told to stop. Housekeeping runs on its own timer, not every tick.

    ``should_stop`` is checked between ticks so a signal handler can end the process
    between commands rather than in the middle of one; a handler interrupted mid-flight
    would leave its command leased until the deadline lapses, which is recoverable but
    slower than simply finishing.
    """
    total = TickReport()
    ticks = 0
    interval = runtime.settings.housekeeping_interval_seconds
    next_housekeeping = 0.0
    while True:
        if should_stop is not None and should_stop():
            return total
        if max_ticks is not None and ticks >= max_ticks:
            return total

        now = time.monotonic()
        due = interval > 0 and now >= next_housekeeping
        if due:
            next_housekeeping = now + interval
        report = run_once(runtime, housekeeping=due)
        total = total + report
        ticks += 1

        if report.leased == 0:
            time.sleep(runtime.settings.poll_interval_seconds)


def _run_tenant(runtime: WorkerRuntime, tenant: TenantRef, *, housekeeping: bool) -> TickReport:
    """Lease this tenant's ready commands, run each, and report each outcome."""
    report = TickReport()
    if housekeeping:
        report = TickReport(
            housekeeping=run_housekeeping(
                runtime,
                tenant_id=tenant.tenant_id,
                on_dead=_dead_letter_recorder(runtime),
            )
        )

    batch = _lease(runtime, tenant)
    for leased in batch:
        # The scope wraps the report as well as the handler, so the "command ... -> OK"
        # line and any dead letter carry the same id as the work they describe. Binding
        # inside `_run_one` instead would leave the outcome line -- the one an operator
        # actually greps for -- as the only part of the command with no id on it.
        with bind_scope(
            leased.correlation_id,
            tenant_id=leased.tenant_id,
            actor_type=ActorType.WORKER.value,
        ):
            result = _run_one(runtime, leased)
            buried = _report(runtime, tenant, leased, result)
        report = report + TickReport(
            leased=1,
            completed=1 if result.completed else 0,
            failed=0 if result.completed else 1,
            dead_letters=1 if buried else 0,
            details=(f"{leased.command_type}:{result.detail}",),
        )
    return report


def _lease(runtime: WorkerRuntime, tenant: TenantRef) -> tuple[LeasedCommand, ...]:
    """Take a batch and commit the lease before any work begins."""
    with runtime.worker_session() as session:
        set_tenant(session, tenant.tenant_id)
        return lease(
            session,
            worker_id=runtime.settings.worker_id,
            limit=runtime.settings.batch_size,
            policy=runtime.retry_policy,
            on_dead=_recorder_for(session, runtime),
        )


def _run_one(runtime: WorkerRuntime, leased: LeasedCommand) -> HandlerResult:
    """Run one command, turning every failure into a code the outbox understands.

    Nothing escapes. A handler that raised has already rolled back its own transaction --
    the session scope sees to that -- so the worst case is a command that is redelivered
    after its backoff, and every handler is written to survive redelivery because each one
    consumes its Execution Grant first.

    The metrics handle comes from the bound scope rather than from a parameter, so a caller
    that has not bound one -- a test calling this directly -- records nothing and is counted
    as ``missing_tenant`` instead of having its work attributed to a guessed tenant. Three
    outcomes are distinguished on the attempts counter and the duration histogram, because
    they mean three different things: ``completed`` is done, ``failed`` is a handler that
    said no and will be retried, and ``error`` is a handler that raised.
    """
    metrics = default_registry().for_current_scope()
    metrics.increment("commerce_worker_leases_total", command_type=leased.command_type)
    try:
        with timed(metrics, WORKER_COMMAND_TIMING, command_type=leased.command_type) as span:
            result = dispatch(runtime, leased)
            span.set_outcome("completed" if result.completed else "failed")
            return result
    except Exception as exc:
        _LOG.warning(
            "command %s (%s) failed: %s",
            leased.command_id,
            leased.command_type,
            type(exc).__name__,
            exc_info=True,
        )
        return HandlerResult(code=_code_for(exc), detail=reason_key(type(exc).__name__))


def dispatch(runtime: WorkerRuntime, leased: LeasedCommand) -> HandlerResult:
    """Parse a leased row into its typed command and run the handler for that type.

    The match is on the command's class, which is what ADR 0003 asks for and what lets
    mypy narrow each branch to a concrete command type -- so a handler cannot be handed
    the wrong payload shape and have it only surface at runtime, against a real payment.
    """
    command = parse_leased_command(leased)
    match command:
        case ReserveDebitCommand():
            return handle_reserve(runtime, command)
        case CreateOrderCommand():
            return handle_create_order(runtime, command)
        case ApplyWebhookEventCommand():
            return handle_apply_webhook(runtime, command, outbox_command_id=leased.command_id)
        case ReconcilePaymentCommand():
            return handle_reconcile_payment(runtime, command)
        case RefundExecuteCommand():
            return handle_refund_execute(runtime, command)
        case ReconcileRefundCommand():
            return handle_reconcile_refund(runtime, command)
        case _:  # pragma: no cover - parse_command refuses an unknown type first
            raise HandlerError(
                f"no handler for command type {leased.command_type!r}",
                code=RecoveryCode.POLICY_EXCEPTION,
            )


def _report(
    runtime: WorkerRuntime, tenant: TenantRef, leased: LeasedCommand, result: HandlerResult
) -> bool:
    """Complete or fail the outbox row. Returns whether the command was buried.

    A lost lease is reported by the outbox as a code rather than an exception and is not
    an error here: the command was leased to somebody else and will run again. What must
    not happen is silence, so every outcome is logged with the command id.
    """
    with runtime.worker_session() as session:
        set_tenant(session, tenant.tenant_id)
        if result.completed:
            outcome = complete(session, leased.command_id, leased.lease_token)
        else:
            outcome = fail(
                session,
                leased.command_id,
                leased.lease_token,
                code=result.code,
                policy=runtime.retry_policy,
                on_dead=_recorder_for(session, runtime),
            )
        _LOG.info(
            "command %s (%s) -> %s [%s] outbox=%s",
            leased.command_id,
            leased.command_type,
            result.code.value,
            result.detail,
            outcome.status.value,
        )
        return outcome.dead_letter is not None


# ------------------------------------------------------------------- dead letters


def _recorder_for(session: Session, runtime: WorkerRuntime) -> Callable[[DeadLetter], None]:
    """An ``on_dead`` callback bound to this transaction."""

    def record(letter: DeadLetter) -> None:
        # The letter's own correlation id, not the caller's. A command buried during
        # `lease` -- before any scope is bound -- and one buried during housekeeping both
        # arrive here, and in each case the id that makes the burial investigable is the
        # one the outbox row has carried since admission.
        with bind_scope(
            letter.correlation_id,
            tenant_id=letter.tenant_id,
            actor_type=ActorType.WORKER.value,
        ):
            _record_dead_letter(session, runtime, letter)

    return record


def _record_dead_letter(session: Session, runtime: WorkerRuntime, letter: DeadLetter) -> None:
    """Write the evidence, then the counter. In that order, and never the other way round.

    The audit row is the fact: it commits in the reaping transaction, so a failure to
    record the burial rolls the burial back. The counter only says to go and look at that
    row, and it is bumped after the append precisely so that nothing about whether a
    command is buried can depend on whether a metric was recorded (ADR 0007 D3).
    """
    tk.append(
        session,
        tenant=letter.tenant_id,
        aggregate_type=_DEAD_LETTER_AGGREGATE,
        aggregate_id=letter.command_id,
        event_type="outbox.dead_letter",
        actor_type=ActorType.WORKER,
        principal_id=runtime.settings.worker_id,
        payload={
            "command_type": letter.command_type,
            "attempts": letter.attempts,
            "terminal_code": letter.terminal_code.value,
            # The payload is stored so an operator who fixes the cause can revive the
            # command and see exactly what will run. It is already canonical JSON.
            "payload": letter.payload,
        },
        correlation_id=letter.correlation_id,
    )
    _LOG.error(
        "dead letter: command %s (%s) after %d attempts, %s",
        letter.command_id,
        letter.command_type,
        letter.attempts,
        letter.terminal_code.value,
    )
    default_registry().for_tenant(letter.tenant_id).increment(
        "commerce_worker_dead_letters_total",
        command_type=letter.command_type,
        code=letter.terminal_code.value,
    )


def _dead_letter_recorder(runtime: WorkerRuntime) -> Callable[[DeadLetter], None]:
    """An ``on_dead`` callback for a caller that has no session of its own to lend.

    Only housekeeping needs this: :func:`durable_work.reap_exhausted` runs inside the
    worker transaction that ``run_housekeeping`` opens, and this closure opens a second
    one purely to write the audit row. It is the one place where the burial and its
    evidence are not in the same transaction, which is why the reap's own ``on_dead``
    (see :func:`_recorder_for`) is used everywhere else.
    """

    def record(letter: DeadLetter) -> None:
        with runtime.worker_session() as session:
            set_tenant(session, letter.tenant_id)
            _recorder_for(session, runtime)(letter)

    return record


def _code_for(exc: BaseException) -> RecoveryCode:
    """The outbox verdict for an exception that escaped a handler.

    Ordered from most to least specific. The default is deliberately *retryable*: an
    unclassified failure is almost always infrastructure -- a dropped connection, a lock
    timeout -- and redelivering is safe for every handler here because each consumes its
    grant before it sends. A payload the worker cannot parse is the opposite: it will not
    parse next time either, so it is buried for a person.
    """
    if isinstance(exc, HandlerError):
        return exc.code
    if isinstance(exc, OutboxUsageError):
        return RecoveryCode.POLICY_EXCEPTION
    code = getattr(exc, "code", None)
    if isinstance(code, RecoveryCode):
        return code
    return RecoveryCode.CONCURRENT_OPERATION
