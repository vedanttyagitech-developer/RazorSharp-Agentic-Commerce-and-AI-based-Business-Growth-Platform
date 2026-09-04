"""Transactional outbox and work leasing, specification 23.2.

The failure this module exists to prevent is the dual write. A handler that commits a
payment attempt and then publishes to a broker has two commits and no atomicity between
them: crash in the gap and the money state moved while the command that was supposed to
act on it never existed. Nobody is left to retry, and nothing records that anything is
missing. The command is lost silently, which in a payments system means a buyer charged
for an order that is never fulfilled, or an approved refund that never leaves.

So there is exactly one commit. :func:`enqueue` writes an ``outbox_events`` row through
the caller's own :class:`~sqlalchemy.orm.Session`, inside the caller's transaction. If
the caller's state change rolls back, the command rolls back with it; if it commits, the
command is durable. This module never calls ``commit()`` or ``rollback()``, and there is
no code path here that talks to a broker.

Pub/Sub and Cloud Tasks still have a job: they *wake* a worker so latency is milliseconds
rather than the poll interval. They are an optimisation. This table is the truth. Delete
the topic and the system is slower; delete this table and the system loses money.

FIVE RULES DECIDE WHETHER THIS IS SAFE
--------------------------------------

1. **One commit, never two.** :func:`enqueue` participates in the caller's transaction.

2. **Two workers never take the same row.** :func:`lease` selects candidates with
   ``FOR UPDATE SKIP LOCKED``, so a row already locked by another worker's open
   transaction is stepped over rather than waited for. Without ``SKIP LOCKED`` the
   second worker blocks and then, once the first commits, re-reads a row it has already
   been handed -- the classic double-delivery of a naive queue.

3. **A lease expires on the database clock.** ``leased_until`` is written as
   ``now() + interval`` by PostgreSQL and compared against ``now()`` by PostgreSQL. No
   application clock is consulted anywhere in this module. A worker that is killed
   mid-command therefore releases its work automatically: the lease lapses and the
   command becomes leasable again. A pod with a skewed clock cannot shorten another
   worker's lease or extend its own.

4. **Retries are bounded and jittered.** Attempts are counted at hand-out time, so a
   command that kills its worker still consumes attempts and cannot loop forever. The
   delay before the next attempt is exponential with full jitter, because a fixed
   backoff synchronises every failed command onto the same instant and reproduces the
   thundering herd against a provider that is already unwell.

5. **Exhaustion is a state, not a deletion.** A command that runs out of attempts moves
   to ``DEAD``. The row keeps its original payload, its attempt count and its
   correlation id, and no code here deletes it. ``DEAD`` is queryable, alertable, and
   reversible by an operator through :func:`revive`. Dropping the message instead would
   convert a visible incident into an invisible one.

DELIVERY MAY DUPLICATE -- CONSUMERS MUST BE IDEMPOTENT
------------------------------------------------------

This is at-least-once delivery, and that is a deliberate choice rather than a limitation
to be engineered away. Exactly-once delivery across a database and a payment provider is
not available: the provider call and the ``DONE`` write cannot be one atomic action, so
some window always exists where the side effect happened and the bookkeeping did not.

Given that window, the only safe direction to fail is towards *repeating* the command,
never towards *skipping* it. A repeated command is harmless when the consumer is
idempotent; a skipped command is a refund that never arrives. So:

* a worker that dies after calling the provider but before :func:`complete` will see the
  command again once its lease lapses;
* a worker whose lease lapsed mid-command is refused by :func:`complete` (the fencing
  token no longer matches), and the command is redelivered on purpose.

Every consumer of this outbox MUST therefore be idempotent with respect to the command's
payload -- in this platform, by consuming a single-use Execution Grant, or by presenting
the idempotency key recorded alongside the command. A consumer that is not idempotent
will double-charge, and no amount of care in this module can prevent that.

FENCING WITHOUT A ``leased_by`` COLUMN
-------------------------------------

``outbox_events`` has no column naming the worker that holds a lease, so this module
fences with ``leased_until`` itself. :func:`lease` returns the exact ``leased_until``
PostgreSQL wrote as an opaque :class:`LeaseToken`, and every subsequent write requires
that value to still be the row's ``leased_until``. Any re-lease writes a new deadline, so
a worker that was superseded while it was stalled cannot mark another worker's in-flight
command ``DONE``. See the module report: a ``leased_by`` column would add operator
visibility, but it is not what makes this correct.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from commerce_domain import CanonicalizationError, DomainError, canonicalize, uuid7
from platform_db import require_tenant
from sqlalchemy import Row, TextClause, text
from sqlalchemy.orm import Session
from transaction_kernel.recovery import NOT_A_SUCCESS, RETRYABLE, RecoveryCode

__all__ = [
    "DEFAULT_POLICY",
    "MAX_ATTEMPT_CEILING",
    "MAX_BATCH",
    "MAX_LEASE_SECONDS",
    "DeadLetter",
    "DeliveryOutcome",
    "Jitter",
    "LeaseToken",
    "LeasedCommand",
    "OutboxCommand",
    "OutboxError",
    "OutboxStatus",
    "OutboxUsageError",
    "RetryPolicy",
    "backoff_seconds",
    "complete",
    "enqueue",
    "extend_lease",
    "fail",
    "lease",
    "reap_exhausted",
    "revive",
]

# Every SQL string below is composed from module-level literals only. No value taken
# from a request, a model, or a database row is ever joined into SQL text; everything
# variable travels as a bound parameter.

#: Upper bound on how long one worker may hold a command before the lease lapses. A
#: lease longer than this is indistinguishable from a stuck command: the point of the
#: deadline is that a crashed worker releases its work, and an hour-long deadline means
#: an hour of a refund sitting still. Long jobs heartbeat with :func:`extend_lease`.
MAX_LEASE_SECONDS: Final = 3_600

#: Hard ceiling on ``max_attempts``. Retrying a provider call hundreds of times is not
#: resilience, it is an outage amplifier pointed at someone else's API.
MAX_ATTEMPT_CEILING: Final = 64

#: Largest batch one :func:`lease` call may take. A worker that leases ten thousand rows
#: owns ten thousand commands it cannot finish before its lease lapses, and every one of
#: them is then redelivered.
MAX_BATCH: Final = 500

#: Longest command_type the schema accepts (``VARCHAR(64)``). Checked here so an
#: over-long type fails with a domain error naming the field rather than a driver error.
MAX_COMMAND_TYPE = 64

#: Longest accepted worker identity. Carried on :class:`LeasedCommand` for logging.
MAX_WORKER_ID = 128

#: Cap on the doubling exponent so a mis-set ``max_attempts`` cannot compute ``2 ** 500``
#: before the ``min`` against the cap has a chance to bound it.
_MAX_EXPONENT: Final = 32

#: Returns a value in ``[0, n)``. Injectable so tests can pin the jitter and assert the
#: envelope exactly; production uses a CSPRNG because there is no reason not to.
Jitter = Callable[[int], int]

#: Opaque fencing token. It is the ``leased_until`` PostgreSQL wrote, and it is only ever
#: produced by this module and handed straight back.
LeaseToken = datetime


class OutboxError(DomainError):
    """An outbox operation was asked for something it cannot honour."""


class OutboxUsageError(OutboxError):
    """The caller misused the outbox API.

    Raised rather than returned as a :class:`RecoveryCode`: these are programming errors
    (a float in a payload, a lease longer than the maximum, reporting failure with a
    success code), not outcomes an agent can recover from by retrying or re-approving.
    Raising aborts the surrounding transaction, which is the fail-closed direction.
    """


class OutboxStatus(StrEnum):
    """The five states the ``outbox_events`` check constraint permits.

    ``PENDING`` and ``FAILED`` are both leasable once ``available_at`` has passed; they
    are kept distinct so that "never attempted" and "attempted and backing off" can be
    told apart on a dashboard. A rising ``FAILED`` count is a provider incident; a rising
    ``PENDING`` count is a worker shortage, and the two have opposite remedies.
    """

    PENDING = "PENDING"
    LEASED = "LEASED"
    DONE = "DONE"
    FAILED = "FAILED"
    DEAD = "DEAD"


#: Statuses from which a command can still be handed to a worker.
LEASABLE: Final[frozenset[OutboxStatus]] = frozenset(
    {OutboxStatus.PENDING, OutboxStatus.FAILED, OutboxStatus.LEASED}
)

#: Statuses no worker will act on again without an operator.
TERMINAL: Final[frozenset[OutboxStatus]] = frozenset({OutboxStatus.DONE, OutboxStatus.DEAD})


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How hard, how often, and for how long this outbox retries one command.

    Defaults are deliberately modest. Eight attempts with a two-second base and an hour
    cap spans roughly four hours of provider outage before a command is escalated to a
    human, which is long enough to ride out a regional incident and short enough that a
    stuck refund is noticed the same working day.
    """

    max_attempts: int = 8
    lease_seconds: int = 60
    backoff_base_seconds: int = 2
    backoff_cap_seconds: int = 3_600
    jitter: Jitter = field(default=secrets.randbelow)

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= MAX_ATTEMPT_CEILING:
            raise OutboxUsageError(
                f"max_attempts must be in 1..{MAX_ATTEMPT_CEILING}, got {self.max_attempts}"
            )
        if not 1 <= self.lease_seconds <= MAX_LEASE_SECONDS:
            raise OutboxUsageError(
                f"lease_seconds must be in 1..{MAX_LEASE_SECONDS}, got {self.lease_seconds}"
            )
        if self.backoff_base_seconds < 1:
            raise OutboxUsageError("backoff_base_seconds must be at least 1 second")
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            # A cap below the base would silently make every retry fire at the cap, which
            # is a fixed backoff wearing the costume of an exponential one.
            raise OutboxUsageError("backoff_cap_seconds cannot be below backoff_base_seconds")


DEFAULT_POLICY: Final = RetryPolicy()


def backoff_seconds(attempts: int, policy: RetryPolicy = DEFAULT_POLICY) -> int:
    """Seconds to wait before delivery attempt ``attempts + 1``.

    Guarantees: the result is an integer in ``[1, ceiling]`` where ``ceiling`` is
    ``min(cap, base * 2 ** (attempts - 1))``; it is never zero, so a permanently failing
    command cannot become a hot loop against the database.

    Refuses: ``attempts`` below 1, because there is no delay before a first attempt that
    has not happened yet.

    The jitter is *full* jitter -- uniform across the whole envelope rather than a small
    perturbation of it. When a provider fails a thousand commands in one second, a
    deterministic backoff schedules a thousand identical retries at the same instant and
    keeps doing so at every level of the exponential. Full jitter spreads them, which is
    the difference between probing a recovering provider and re-DDoSing it.
    """
    if attempts < 1:
        raise OutboxUsageError(f"attempts must be at least 1 to compute a backoff, got {attempts}")
    envelope = policy.backoff_base_seconds * 2 ** min(attempts - 1, _MAX_EXPONENT)
    ceiling = min(policy.backoff_cap_seconds, envelope)
    # randbelow(n) yields [0, n); the +1 shifts it to [1, ceiling] so the floor is one
    # whole second rather than an immediate re-lease.
    return 1 + policy.jitter(ceiling)


# --------------------------------------------------------------------------- values


@dataclass(frozen=True, slots=True)
class OutboxCommand:
    """A command as it was committed. Returned by :func:`enqueue`.

    ``available_at`` and ``created_at`` are the database's own timestamps, read back from
    the insert rather than computed here, so a caller cannot record a schedule its own
    clock believes in but PostgreSQL does not.
    """

    command_id: uuid.UUID
    tenant_id: uuid.UUID
    command_type: str
    status: OutboxStatus
    attempts: int
    available_at: datetime
    correlation_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LeasedCommand:
    """One command handed to one worker until ``lease_token`` passes.

    ``lease_token`` is the fence. Hand it back to :func:`complete`, :func:`fail` or
    :func:`extend_lease`; a stale token is refused, which is what stops a stalled worker
    from finishing a command that has already been reassigned.

    ``attempts`` is the delivery attempt this lease represents, counted from 1. It is
    already incremented, so a handler can log "attempt 3 of 8" without a second query.
    """

    command_id: uuid.UUID
    tenant_id: uuid.UUID
    command_type: str
    payload: dict[str, Any]
    attempts: int
    lease_token: LeaseToken
    worker_id: str
    correlation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """The evidence produced when a command is buried.

    Carries the command's original payload verbatim. The point of a dead letter is that
    an operator can see exactly what was asked for and, after fixing the cause, put it
    back with :func:`revive`; a dead letter that summarised the payload instead would
    make that impossible.
    """

    command_id: uuid.UUID
    tenant_id: uuid.UUID
    command_type: str
    payload: dict[str, Any]
    attempts: int
    terminal_code: RecoveryCode
    correlation_id: uuid.UUID
    died_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """The structured answer to :func:`complete` and :func:`fail`. Never prose.

    ``code`` is what the worker should act on, ``status`` is where the row actually
    landed, and the two are reported separately because they answer different questions:
    a worker asks "may I move on?", an operator asks "what is in the table?".
    """

    code: RecoveryCode
    status: OutboxStatus
    dead_letter: DeadLetter | None = None
    retry_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.code is RecoveryCode.OK


# ------------------------------------------------------------------------------ SQL


def _stmt(*parts: str) -> TextClause:
    """Compose one statement from the literal fragments written in this module.

    Every argument is a literal or a module-level constant. Nothing arriving from a
    request, a model, or a database row is ever joined into SQL text here.
    """
    return text(" ".join(parts))


# The row shape every statement returns, so one mapper covers all of them. ``now()`` is
# projected as ``observed_at`` because the burial timestamp must be the database's.
_PROJECTION: Final = """
        id,
        tenant_id,
        command_type,
        payload,
        status,
        attempts,
        available_at,
        leased_until,
        correlation_id,
        created_at,
        now() AS observed_at
"""

_INSERT = _stmt(
    "INSERT INTO outbox_events",
    "  (id, tenant_id, command_type, payload, status, attempts, available_at, correlation_id)",
    "VALUES (:id, :tenant_id, :command_type, CAST(:payload AS jsonb), 'PENDING', 0,",
    "        now() + (CAST(:delay_seconds AS integer) * INTERVAL '1 second'), :correlation_id)",
    "RETURNING",
    _PROJECTION,
)

# Readiness, decided entirely by PostgreSQL's clock:
#   * PENDING/FAILED whose backoff has elapsed, or
#   * LEASED whose holder's deadline has passed -- rule 3, the crashed-worker release.
# ``attempts < :max_attempts`` keeps an exhausted command out of the pool; it is buried
# by _REAP rather than handed out again.
_READY_PREDICATE: Final = """
          attempts < CAST(:max_attempts AS integer)
      AND (
               (status IN ('PENDING', 'FAILED') AND available_at <= now())
            OR (status = 'LEASED' AND leased_until <= now())
          )
"""

# WHY A MATERIALIZED CTE AND NOT `WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP
# LOCKED)`: the IN form does not reliably honour its own LIMIT. `FOR UPDATE` stops the
# planner hashing the subquery, so it can end up as a SubPlan re-executed once per
# candidate row of the outer scan -- and each execution takes a *fresh* batch, because
# the rows the previous execution locked are this transaction's own and are not skipped.
# The observed symptom on PostgreSQL 16 with row-level security enabled was `limit=1`
# leasing two rows. A worker that leases more than its batch owns commands it cannot
# finish inside the lease, so every excess row is redelivered; at scale that is a
# self-inflicted duplicate-delivery storm. `AS MATERIALIZED` forces the candidate set to
# be computed exactly once, which is the only form of this statement that is safe.
#
# FOR UPDATE SKIP LOCKED is the whole of rule 2. Candidate rows already locked by another
# worker's open transaction are stepped over, so N workers partition the queue instead of
# queueing behind each other and then re-reading rows that were already handed out.
# ``attempts`` is incremented here, at hand-out, not at failure: a command that crashes
# its worker before it can report anything must still consume an attempt, otherwise a
# poison payload loops forever and no bound on attempts means anything.
_LEASE = _stmt(
    "WITH ready AS MATERIALIZED (",
    "    SELECT id AS ready_id FROM outbox_events",
    "    WHERE",
    _READY_PREDICATE,
    "    ORDER BY available_at, id",
    "    LIMIT CAST(:limit AS integer)",
    "    FOR UPDATE SKIP LOCKED",
    ")",
    "UPDATE outbox_events AS o",
    "SET status = 'LEASED',",
    "    attempts = o.attempts + 1,",
    "    leased_until = now() + (CAST(:lease_seconds AS integer) * INTERVAL '1 second')",
    "FROM ready WHERE o.id = ready.ready_id",
    "RETURNING",
    _PROJECTION,
)

# The fence. ``leased_until = :token`` proves this caller still holds the lease it was
# given, and ``leased_until > now()`` proves the lease has not lapsed underneath it. Both
# are needed: the first stops a superseded worker from writing over its successor, the
# second stops a stalled worker from writing at all once its deadline has passed, because
# at that moment another worker may already be reading the row.
_FENCE: Final = """
      id = :id
  AND status = 'LEASED'
  AND leased_until = CAST(:token AS timestamptz)
  AND leased_until > now()
"""

# leased_until is deliberately left in place on the terminal transitions. It costs
# nothing and lets a late call from the buried worker be told DUPLICATE_OPERATION or
# HUMAN_REVIEW_REQUIRED instead of the misleading CONCURRENT_OPERATION.
_COMPLETE = _stmt(
    "UPDATE outbox_events SET status = 'DONE' WHERE", _FENCE, "RETURNING", _PROJECTION
)

_EXTEND = _stmt(
    "UPDATE outbox_events",
    "SET leased_until = now() + (CAST(:lease_seconds AS integer) * INTERVAL '1 second')",
    "WHERE",
    _FENCE,
    "RETURNING",
    _PROJECTION,
)

_LOCK_FENCED = _stmt("SELECT", _PROJECTION, "FROM outbox_events WHERE", _FENCE, "FOR UPDATE")

_SCHEDULE_RETRY = _stmt(
    "UPDATE outbox_events",
    "SET status = 'FAILED',",
    "    available_at = now() + (CAST(:delay_seconds AS integer) * INTERVAL '1 second')",
    "WHERE id = :id",
    "RETURNING",
    _PROJECTION,
)

_BURY = _stmt("UPDATE outbox_events SET status = 'DEAD' WHERE id = :id RETURNING", _PROJECTION)

# Rows that ran out of attempts without anyone reporting why -- in practice a worker that
# died while holding the lease on its last attempt. Without this sweep such a row would
# sit in LEASED with a lapsed deadline, invisible to _LEASE (attempts exhausted) and to
# every dead-letter alert, which is precisely the silent loss rule 5 forbids.
# A LEASED row whose deadline has NOT passed is left alone: its worker may yet succeed.
_REAP = _stmt(
    "WITH exhausted AS MATERIALIZED (",
    "    SELECT id AS ready_id FROM outbox_events",
    "    WHERE status IN ('PENDING', 'FAILED', 'LEASED')",
    "      AND attempts >= CAST(:max_attempts AS integer)",
    "      AND (status <> 'LEASED' OR leased_until <= now())",
    "    ORDER BY created_at, id",
    "    LIMIT CAST(:limit AS integer)",
    "    FOR UPDATE SKIP LOCKED",
    ")",
    "UPDATE outbox_events AS o SET status = 'DEAD'",
    "FROM exhausted WHERE o.id = exhausted.ready_id",
    "RETURNING",
    _PROJECTION,
)

_REVIVE = _stmt(
    "UPDATE outbox_events",
    "SET status = 'PENDING', attempts = 0, available_at = now(), leased_until = NULL",
    "WHERE id = :id AND status = 'DEAD'",
    "RETURNING",
    _PROJECTION,
)

_SELECT_ONE = _stmt("SELECT", _PROJECTION, "FROM outbox_events WHERE id = :id")


# ----------------------------------------------------------------------- internals


def _leased(row: Row[Any], worker_id: str) -> LeasedCommand:
    return LeasedCommand(
        command_id=row.id,
        tenant_id=row.tenant_id,
        command_type=row.command_type,
        payload=row.payload,
        attempts=int(row.attempts),
        lease_token=row.leased_until,
        worker_id=worker_id,
        correlation_id=row.correlation_id,
    )


def _dead_letter(row: Row[Any], terminal_code: RecoveryCode) -> DeadLetter:
    return DeadLetter(
        command_id=row.id,
        tenant_id=row.tenant_id,
        command_type=row.command_type,
        payload=row.payload,
        attempts=int(row.attempts),
        terminal_code=terminal_code,
        correlation_id=row.correlation_id,
        # The database's clock, projected by the same statement that moved the row.
        died_at=row.observed_at,
    )


def _validate_payload(payload: dict[str, Any]) -> str:
    """Reject a payload that cannot be a command, before it reaches the table.

    Canonicalisation is used as the validator on purpose. The integer-only JCS profile
    refuses ``float``, so a rupee amount that escaped :class:`commerce_domain.Money` and
    arrived as ``149.90`` is rejected at enqueue rather than committed, woken, and paid
    out with a rounding error some hours later. The canonical form is also stable, which
    is what makes a stored payload comparable to the one that was approved.
    """
    if not isinstance(payload, dict):
        raise OutboxUsageError(f"payload must be a JSON object, got {type(payload).__name__}")
    try:
        canonical: str = canonicalize(payload).decode("utf-8")
    except CanonicalizationError as exc:
        raise OutboxUsageError(f"payload is not a valid command body: {exc}") from exc
    return canonical


def _bury(
    session: Session,
    command_id: uuid.UUID,
    terminal_code: RecoveryCode,
    on_dead: Callable[[DeadLetter], None] | None,
) -> DeadLetter:
    """Move one row to DEAD and hand the evidence to the audit hook.

    ``on_dead`` runs inside the caller's transaction, before this function returns. That
    is deliberate: if recording the burial fails, the burial itself must not commit,
    otherwise the platform ends up with a command nobody will ever run and no record that
    it existed -- exactly the silent loss the DEAD state was introduced to prevent.
    """
    row = session.execute(_BURY, {"id": command_id}).one()
    letter = _dead_letter(row, terminal_code)
    if on_dead is not None:
        on_dead(letter)
    return letter


def _lost_lease_outcome(session: Session, command_id: uuid.UUID) -> DeliveryOutcome:
    """Explain why a fenced write matched nothing.

    Called only after the fence failed, so the row is in some state this caller no longer
    owns. Each answer tells the worker something different about what to do next.
    """
    row = session.execute(_SELECT_ONE, {"id": command_id}).one_or_none()
    if row is None:
        # Nothing deletes from this table, so a missing row means the id was never in it,
        # or RLS is hiding another tenant's row. Both are bugs in the caller, and both
        # must be loud: swallowing them would let a worker silently drop a command.
        raise OutboxUsageError(
            f"outbox command {command_id} is not visible in this transaction; "
            "check the command id and the tenant bound to this session"
        )
    status = OutboxStatus(row.status)
    if status is OutboxStatus.DONE:
        # Someone already finished this command. At-least-once delivery makes this an
        # expected outcome, not an error: the work is done, the worker moves on.
        return DeliveryOutcome(RecoveryCode.DUPLICATE_OPERATION, status)
    if status is OutboxStatus.DEAD:
        return DeliveryOutcome(RecoveryCode.HUMAN_REVIEW_REQUIRED, status)
    # PENDING, FAILED, or LEASED under a newer deadline: this worker's lease lapsed and
    # the command is (or soon will be) someone else's. Retryable, by another lease.
    return DeliveryOutcome(RecoveryCode.CONCURRENT_OPERATION, status, retry_at=row.available_at)


# -------------------------------------------------------------------------- the API


def enqueue(
    session: Session,
    *,
    command_type: str,
    payload: dict[str, Any],
    correlation_id: uuid.UUID,
    available_in_seconds: int = 0,
) -> OutboxCommand:
    """Record a command in the caller's transaction. Never commits.

    Guarantees: the row is written through ``session``, so it becomes durable if and only
    if the caller's own state change does. There is no second write to a broker and no
    commit here, which is what makes "the payment attempt exists but its command does
    not" unrepresentable.

    Refuses: a session with no tenant bound (row-level security would reject the insert
    anyway, but a :class:`~platform_db.TenantContextError` names the real cause); a
    payload that is not a JSON object, or that contains a float -- money in a command
    payload is integer minor units, and a float there is a rounding error waiting for a
    provider call; a blank or over-long ``command_type``; a ``correlation_id`` that is
    not a :class:`uuid.UUID`; a negative delay.

    ``available_in_seconds`` delays first delivery, measured from the database clock at
    commit. Use it for a command that must not run immediately (a scheduled sweep, a
    deliberate settle-down after a provider error), never as a substitute for the retry
    backoff, which this module manages itself.
    """
    tenant_id = require_tenant(session)
    if not command_type or not command_type.strip():
        raise OutboxUsageError("command_type must be a non-empty identifier")
    if len(command_type) > MAX_COMMAND_TYPE:
        raise OutboxUsageError(
            f"command_type exceeds {MAX_COMMAND_TYPE} characters: {command_type!r}"
        )
    if not isinstance(correlation_id, uuid.UUID):
        # Checked here for the same reason command_type is: the correlation id is the
        # thread an operator pulls to reconstruct one buyer's journey across the audit
        # log, the payment attempt and this command. A malformed one must name itself,
        # not surface hours later as a psycopg InvalidTextRepresentation on $6.
        raise OutboxUsageError(
            f"correlation_id must be a UUID, got {type(correlation_id).__name__}"
        )
    if isinstance(available_in_seconds, bool) or not isinstance(available_in_seconds, int):
        raise OutboxUsageError("available_in_seconds must be an int number of seconds")
    if available_in_seconds < 0:
        raise OutboxUsageError("available_in_seconds cannot be negative")

    row = session.execute(
        _INSERT,
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "command_type": command_type,
            "payload": _validate_payload(payload),
            "delay_seconds": available_in_seconds,
            "correlation_id": correlation_id,
        },
    ).one()
    return OutboxCommand(
        command_id=row.id,
        tenant_id=row.tenant_id,
        command_type=row.command_type,
        status=OutboxStatus(row.status),
        attempts=int(row.attempts),
        available_at=row.available_at,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
    )


def lease(
    session: Session,
    *,
    worker_id: str,
    limit: int,
    policy: RetryPolicy = DEFAULT_POLICY,
    on_dead: Callable[[DeadLetter], None] | None = None,
) -> tuple[LeasedCommand, ...]:
    """Take up to ``limit`` ready commands for this worker until the lease lapses.

    Guarantees: no other worker can be handed a row this call returns, because candidates
    are locked with ``FOR UPDATE SKIP LOCKED`` inside this transaction -- a concurrent
    worker steps over them rather than waiting and re-reading them. A command whose
    previous holder crashed is picked up once its ``leased_until`` passes on the database
    clock. Every returned command has had ``attempts`` incremented, so the attempt bound
    holds even for a command that never reports back.

    Before selecting, this call buries any command that has already exhausted its
    attempts and is not currently held by a live lease (see :func:`reap_exhausted`).
    Doing it here rather than in a separate cron means there is no configuration under
    which an exhausted command sits in the table forever with nobody watching it.

    Refuses: a session with no tenant bound; a blank or over-long ``worker_id``; a
    ``limit`` outside ``1..MAX_BATCH``.

    The returned rows are leased but not yet visible to anyone else *until this
    transaction commits*. Commit before starting work: an open transaction holds the
    row locks, and a worker that performs a provider call with its lease transaction
    still open holds those locks for the duration of the network round trip.
    """
    require_tenant(session)
    if not worker_id or not worker_id.strip():
        raise OutboxUsageError("worker_id must be a non-empty identifier")
    if len(worker_id) > MAX_WORKER_ID:
        raise OutboxUsageError(f"worker_id exceeds {MAX_WORKER_ID} characters")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise OutboxUsageError("limit must be an int")
    if not 1 <= limit <= MAX_BATCH:
        raise OutboxUsageError(f"limit must be in 1..{MAX_BATCH}, got {limit}")

    reap_exhausted(session, policy=policy, limit=limit, on_dead=on_dead)
    rows = session.execute(
        _LEASE,
        {
            "lease_seconds": policy.lease_seconds,
            "max_attempts": policy.max_attempts,
            "limit": limit,
        },
    ).all()
    return tuple(_leased(row, worker_id) for row in rows)


def extend_lease(
    session: Session,
    command_id: uuid.UUID,
    lease_token: LeaseToken,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
) -> LeaseToken | None:
    """Push this worker's deadline out and return the new fencing token, or None.

    Guarantees: the extension applies only while this caller still holds a live lease, so
    a worker cannot resurrect a lease that has already lapsed and been reassigned. The
    new deadline is computed by PostgreSQL from its own clock.

    Refuses: any call whose token no longer matches the row, or whose lease has already
    expired -- both return ``None``, meaning "you no longer own this command; stop".

    This is the correct way to handle a genuinely long command. Raising
    ``lease_seconds`` instead makes every crashed worker's command sit idle for that
    longer period.
    """
    row = session.execute(
        _EXTEND,
        {"id": command_id, "token": lease_token, "lease_seconds": policy.lease_seconds},
    ).one_or_none()
    return None if row is None else row.leased_until


def complete(session: Session, command_id: uuid.UUID, lease_token: LeaseToken) -> DeliveryOutcome:
    """Mark a command delivered. Only the live lease holder may.

    Guarantees: the row moves to ``DONE`` only if ``lease_token`` is still the row's
    ``leased_until`` and that deadline has not passed. A worker that stalled past its
    lease is refused, and the command is redelivered instead -- see the module docstring:
    when the provider call may or may not have landed, repeating it is recoverable by an
    idempotent consumer and skipping it is not.

    Refuses (as codes, not exceptions): ``DUPLICATE_OPERATION`` when the command was
    already completed, ``HUMAN_REVIEW_REQUIRED`` when it was buried while this worker
    held it, ``CONCURRENT_OPERATION`` when the lease was lost.

    Never commits. The caller commits, which is what makes "provider called, command
    still PENDING" a short window rather than a lost command.
    """
    row = session.execute(_COMPLETE, {"id": command_id, "token": lease_token}).one_or_none()
    if row is None:
        return _lost_lease_outcome(session, command_id)
    return DeliveryOutcome(RecoveryCode.OK, OutboxStatus.DONE)


def fail(
    session: Session,
    command_id: uuid.UUID,
    lease_token: LeaseToken,
    *,
    code: RecoveryCode,
    policy: RetryPolicy = DEFAULT_POLICY,
    on_dead: Callable[[DeadLetter], None] | None = None,
) -> DeliveryOutcome:
    """Report that a command did not succeed, and schedule or bury it accordingly.

    Guarantees:

    * A code outside :data:`transaction_kernel.recovery.RETRYABLE` buries the command
      immediately, however many attempts remain. ``PAYMENT_UNKNOWN`` is the case that
      matters: an unknown payment outcome is reconciled, never retried, because retrying
      it is how a buyer gets charged twice for one order.
    * A retryable code with attempts remaining moves the row to ``FAILED`` with
      ``available_at`` set from the database clock plus :func:`backoff_seconds`.
    * A retryable code on the last attempt buries the command. ``DEAD`` keeps the payload
      and the attempt count; nothing is deleted.
    * ``on_dead`` runs inside this transaction, so a failure to record the burial rolls
      the burial back rather than losing the command quietly.

    Refuses: ``RecoveryCode.OK`` and ``DUPLICATE_OPERATION`` -- both mean the command
    succeeded, and reporting a success through the failure path would consume an attempt
    and schedule a pointless retry; call :func:`complete` instead. A stale or expired
    lease token is refused as a code, exactly as in :func:`complete`.
    """
    if code not in NOT_A_SUCCESS:
        raise OutboxUsageError(
            f"{code} reports a completed command; report it with complete(), not fail()"
        )

    row = session.execute(_LOCK_FENCED, {"id": command_id, "token": lease_token}).one_or_none()
    if row is None:
        return _lost_lease_outcome(session, command_id)

    attempts = int(row.attempts)
    exhausted = attempts >= policy.max_attempts
    if code not in RETRYABLE or exhausted:
        letter = _bury(session, command_id, code, on_dead)
        return DeliveryOutcome(RecoveryCode.HUMAN_REVIEW_REQUIRED, OutboxStatus.DEAD, letter)

    delay = backoff_seconds(attempts, policy)
    scheduled = session.execute(_SCHEDULE_RETRY, {"id": command_id, "delay_seconds": delay}).one()
    # The caller's own code is echoed back: the command failed for that reason and is
    # merely queued to try again. Reporting OK here would tell a worker the failure was
    # handled away, which it was not.
    return DeliveryOutcome(code, OutboxStatus.FAILED, retry_at=scheduled.available_at)


def reap_exhausted(
    session: Session,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    limit: int = MAX_BATCH,
    on_dead: Callable[[DeadLetter], None] | None = None,
) -> tuple[DeadLetter, ...]:
    """Bury commands that ran out of attempts without anyone reporting a reason.

    Guarantees: only rows whose attempts are exhausted AND which are not held by a live
    lease are buried, so a worker still legitimately working on its final attempt is left
    alone. Rows locked by a concurrent reaper are skipped rather than waited for. Each
    burial calls ``on_dead`` inside this transaction.

    The buried rows carry ``HUMAN_REVIEW_REQUIRED`` as their terminal code because that
    is the literal truth: nobody said why. In practice these are commands whose worker
    died while holding the lease on its last attempt. Without this sweep such a row would
    be excluded from :func:`lease` by the attempt bound and left in ``LEASED`` forever --
    a lost command that no dead-letter alert would ever see.

    :func:`lease` calls this first, so a deployment gets the guarantee without having to
    remember to schedule it. Call it directly only for a dedicated sweeper.

    Refuses: a session with no tenant bound. A sweeper is the one caller with nobody
    downstream to notice it did nothing: under RLS an unbound tenant matches no rows, so
    it would report "nothing exhausted" on every pass while dead commands accumulated in
    ``LEASED`` -- the silent loss this function exists to prevent, produced by the
    function itself.
    """
    require_tenant(session)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise OutboxUsageError("limit must be an int")
    if not 1 <= limit <= MAX_BATCH:
        raise OutboxUsageError(f"limit must be in 1..{MAX_BATCH}, got {limit}")

    rows = session.execute(_REAP, {"max_attempts": policy.max_attempts, "limit": limit}).all()
    letters = tuple(_dead_letter(row, RecoveryCode.HUMAN_REVIEW_REQUIRED) for row in rows)
    if on_dead is not None:
        for letter in letters:
            on_dead(letter)
    return letters


def revive(session: Session, command_id: uuid.UUID) -> DeliveryOutcome:
    """Return one buried command to the queue with a fresh attempt budget.

    Guarantees: only a ``DEAD`` row is revived, and the payload is untouched -- the
    command that runs is the command that was originally committed, not an operator's
    reconstruction of it. Attempts reset to zero and the row becomes immediately
    available on the database clock.

    Refuses: any row not in ``DEAD``, reported as ``CONCURRENT_OPERATION`` with the
    status actually found. Reviving a ``DONE`` command would re-run a completed money
    operation, and reviving a ``LEASED`` one would race the worker holding it.

    This is an operator action taken after the cause has been fixed. It is the reason a
    dead letter is a state rather than a deletion: there is a way back.
    """
    row = session.execute(_REVIVE, {"id": command_id}).one_or_none()
    if row is not None:
        return DeliveryOutcome(RecoveryCode.OK, OutboxStatus.PENDING, retry_at=row.available_at)
    current = session.execute(_SELECT_ONE, {"id": command_id}).one_or_none()
    if current is None:
        raise OutboxUsageError(
            f"outbox command {command_id} is not visible in this transaction; "
            "check the command id and the tenant bound to this session"
        )
    return DeliveryOutcome(RecoveryCode.CONCURRENT_OPERATION, OutboxStatus(current.status))
