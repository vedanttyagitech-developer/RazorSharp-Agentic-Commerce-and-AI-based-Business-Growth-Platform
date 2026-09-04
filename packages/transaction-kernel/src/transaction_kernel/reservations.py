"""Reservation lifecycle, specification 10.4.

A reservation is a temporary hold on scarce merchant state, taken between quote and
approval and spent at admission. Four rules decide whether it is safe:

1. **Validity is decided by the database clock.** Every expiry comparison in this module
   is ``expires_at`` against SQL ``now()``, inside the same transaction that reads the
   row. Nothing here calls an application clock. A pod whose clock runs an hour slow
   would otherwise admit an hour-expired hold and oversell stock it no longer owns; a
   pod running fast would cancel holds buyers still legitimately have.

2. **The cleanup worker is hygiene, not authority.** ``sweep_expired`` exists so that
   ``status`` eventually reflects reality and the table stays small. It is never a
   precondition for correctness: an expired row still sitting in ``ACTIVE`` because no
   worker has run is refused at admission exactly as if it had been swept. If a sweep
   were load-bearing, every worker outage would become an overselling incident.

3. **An unknown payment outcome holds the reservation.** Releases happen on confirmed
   failure, on cancellation, and on real expiry. They do not happen while a payment
   outcome is ``UNKNOWN`` or under reconciliation, because "we do not know" is not
   "it failed": releasing there can resell inventory that was in fact paid for, and the
   buyer then owns a charge the merchant cannot fulfil. The hold is kept until
   reconciliation resolves the outcome (specification 6.4.1, 10.4).

4. **Concurrent reservers of one scarce item are serialized.** Two agents racing for the
   last unit must not both walk away holding it, and a basket is refused whole rather
   than holding one item while overselling another. See ``reserve`` and ``Allocation``.

Scope note. The transaction-path schema has no ``inventory_positions`` table yet; it
arrives with the catalogue service. Until it does, held quantity is derived from the
immutable, hash-bound ``checkout_versions.content`` of each live reservation, and mutual
exclusion comes from a transaction-scoped advisory lock rather than a row lock on an
inventory row. When ``inventory_positions`` lands, ``_HELD_UNITS`` should become a
``SELECT ... FOR UPDATE`` against that row and the advisory lock should be deleted; the
public surface of this module does not need to change.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from commerce_domain import DomainError, uuid7
from platform_db import require_tenant
from sqlalchemy import Row, TextClause, text
from sqlalchemy.orm import Session

from .recovery import RecoveryCode

# Every SQL string in this module is built from module-level constants only. No value
# read from a request, a model, or the database is ever interpolated into SQL text;
# everything variable is a bound parameter. See `_stmt`.

__all__ = [
    "MAX_TTL_SECONDS",
    "Allocation",
    "ReleaseCause",
    "ReservationContractError",
    "ReservationError",
    "ReservationOutcome",
    "ReservationStatus",
    "ReservationView",
    "check_validity",
    "consume",
    "release",
    "reserve",
    "sweep_expired",
]

#: Upper bound on a hold. An unbounded TTL is an oversell with extra steps: stock stays
#: unsellable for as long as someone forgot a unit conversion.
MAX_TTL_SECONDS: Final = 86_400


class ReservationError(DomainError):
    """A reservation operation was asked for something it cannot honour."""


class ReservationContractError(ReservationError):
    """Checkout content could not be read well enough to prove a hold is safe.

    Raised rather than returned: it aborts the surrounding transaction, which is the
    fail-closed direction. The alternative — treating unreadable content as holding zero
    units — silently converts an oversell guard into a no-op.
    """


class ReservationStatus(StrEnum):
    """The four states the ``reservations`` check constraint permits.

    ``ACTIVE`` and ``CONSUMED`` both hold stock. ``RELEASED`` and ``EXPIRED`` are
    terminal and hold nothing.
    """

    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class ReleaseCause(StrEnum):
    """Why a caller is asking for a hold to end.

    The cause is supplied rather than inferred so that the refusal to release under an
    unknown outcome is explicit in the call site and visible in review, instead of
    depending on which branch of a payment handler happened to run.
    """

    PAYMENT_FAILED = "PAYMENT_FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    PAYMENT_UNKNOWN = "PAYMENT_UNKNOWN"
    RECONCILING = "RECONCILING"


#: Causes that end a hold. All three are confirmed facts, not absences of information.
RELEASING_CAUSES: Final[frozenset[ReleaseCause]] = frozenset(
    {ReleaseCause.PAYMENT_FAILED, ReleaseCause.CANCELLED, ReleaseCause.EXPIRED}
)

#: Causes under which the hold is kept. The outcome is unresolved, and an unresolved
#: outcome may still turn out to be a completed payment.
HOLDING_CAUSES: Final[frozenset[ReleaseCause]] = frozenset(
    {ReleaseCause.PAYMENT_UNKNOWN, ReleaseCause.RECONCILING}
)

_HOLDING_CODE: Final[dict[ReleaseCause, RecoveryCode]] = {
    ReleaseCause.PAYMENT_UNKNOWN: RecoveryCode.PAYMENT_UNKNOWN,
    ReleaseCause.RECONCILING: RecoveryCode.RECONCILIATION_IN_PROGRESS,
}


@dataclass(frozen=True, slots=True)
class ReservationView:
    """A reservation as the database sees it, at the instant of the query.

    ``expired`` and ``seconds_remaining`` are computed by PostgreSQL in the same
    statement that read the row. They are not recomputed in Python, so a caller cannot
    accidentally re-derive them from a skewed application clock.
    """

    reservation_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    status: ReservationStatus
    expires_at: datetime
    expired: bool
    seconds_remaining: int

    @property
    def holds_stock(self) -> bool:
        """True while this row still withholds inventory from other buyers."""
        return self.status in (ReservationStatus.ACTIVE, ReservationStatus.CONSUMED)


@dataclass(frozen=True, slots=True)
class Allocation:
    """The scarce item a reservation must hold, and the stock that bounds it.

    ``available_units`` is the stock figure the caller re-read inside the admission
    transaction (specification 10.3 step 8). It is passed in rather than read here
    because merchant stock is not owned by this module.

    The number of units wanted is *not* passed in: it is derived from the immutable
    checkout version being reserved, so the quantity that is checked against stock and
    the quantity the buyer approved cannot drift apart.
    """

    scarcity_key: str
    available_units: int

    def __post_init__(self) -> None:
        if not self.scarcity_key:
            raise ReservationError("scarcity_key must be a non-empty item identifier")
        if isinstance(self.available_units, bool) or not isinstance(self.available_units, int):
            raise ReservationError("available_units must be an int count of whole units")
        if self.available_units < 0:
            raise ReservationError("available_units cannot be negative")


@dataclass(frozen=True, slots=True)
class ReservationOutcome:
    """A structured answer. Never prose, never an exception for an expected refusal."""

    code: RecoveryCode
    reservation: ReservationView | None = None

    @property
    def ok(self) -> bool:
        return self.code is RecoveryCode.OK


# --------------------------------------------------------------------------- SQL

# Projection shared by every statement. `expired` and `seconds_remaining` are evaluated
# against now() by the database, which is the whole point of this module.
_PROJECTION: Final = """
        id,
        checkout_id,
        checkout_version,
        status,
        expires_at,
        (expires_at <= now()) AS expired,
        GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (expires_at - now()))))::bigint
            AS seconds_remaining
"""

# A live hold sorts first, then the newest row. `reserve` permits at most one live hold
# per checkout version, so the first clause normally decides; the rest makes the answer
# deterministic when only terminal rows remain (an expired hold followed by a re-reserve).
_ORDER: Final = """
    ORDER BY (status = 'ACTIVE' AND expires_at > now()) DESC, created_at DESC, id DESC
    LIMIT 1
"""


def _stmt(*parts: str) -> TextClause:
    """Compose one statement from the SQL fragments in this module.

    Every argument is a literal written above or a module-level constant. Nothing that
    arrives from a request, a model, or a database row is ever joined into SQL text
    here: every variable value in this module travels as a bound parameter.
    """
    return text(" ".join(parts))


_SELECT_CURRENT = _stmt(
    "SELECT",
    _PROJECTION,
    "FROM reservations",
    "WHERE checkout_id = :checkout_id AND checkout_version = :checkout_version",
    _ORDER,
)

_SELECT_CURRENT_FOR_UPDATE = _stmt(
    "SELECT",
    _PROJECTION,
    "FROM reservations",
    "WHERE checkout_id = :checkout_id AND checkout_version = :checkout_version",
    _ORDER,
    "FOR UPDATE",
)

_INSERT = _stmt(
    "INSERT INTO reservations",
    "(id, tenant_id, checkout_id, checkout_version, status, expires_at)",
    "VALUES (:id, :tenant_id, :checkout_id, :checkout_version, 'ACTIVE',",
    "        now() + (CAST(:ttl_seconds AS integer) * INTERVAL '1 second'))",
    "RETURNING",
    _PROJECTION,
)

# The `expires_at > now()` here is not redundant with the caller's check: the row lock is
# taken by the preceding SELECT, but the clock still has to be the database's at the
# moment of the write, so that a hold cannot be spent one microsecond after it lapsed.
_CONSUME = _stmt(
    "UPDATE reservations SET status = 'CONSUMED'",
    "WHERE id = :id AND status = 'ACTIVE' AND expires_at > now()",
    "RETURNING",
    _PROJECTION,
)

_RELEASE = _stmt(
    "UPDATE reservations SET status = 'RELEASED'",
    "WHERE id = :id AND status IN ('ACTIVE', 'CONSUMED')",
    "RETURNING",
    _PROJECTION,
)

# Marking a hold EXPIRED requires the database to agree that it has lapsed. Without the
# `expires_at <= now()` guard a fast pod could retire holds buyers still hold, which is
# invariant 1 in the write direction.
_EXPIRE_ONE = _stmt(
    "UPDATE reservations SET status = 'EXPIRED'",
    "WHERE id = :id AND status = 'ACTIVE' AND expires_at <= now()",
    "RETURNING",
    _PROJECTION,
)

_SWEEP = text(
    "UPDATE reservations SET status = 'EXPIRED' WHERE id IN ("
    "  SELECT id FROM reservations"
    "  WHERE status = 'ACTIVE' AND expires_at <= now()"
    "  ORDER BY expires_at"
    "  LIMIT :limit"
    "  FOR UPDATE SKIP LOCKED"
    ") RETURNING id"
)

# Transaction-scoped: released at COMMIT or ROLLBACK, so a crashed pod cannot strand it.
# The key is hashed from tenant + item because advisory locks are cluster-global and are
# not filtered by row-level security. hashtextextended collisions cost false contention
# between unrelated items, never a missed exclusion.
_ADVISORY_XACT_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))")

# Lines of a checkout version, or an empty array when the content does not carry a JSON
# array there. Callers must check `lines_kind` separately; substituting an empty array
# here only keeps jsonb_array_elements from raising before that check can run.
_LINES_OF_CONTENT: Final = (
    "CASE WHEN jsonb_typeof(content -> 'lines') = 'array' "
    "THEN content -> 'lines' ELSE '[]'::jsonb END"
)
_LINES_OF_CV: Final = (
    "CASE WHEN jsonb_typeof(cv.content -> 'lines') = 'array' "
    "THEN cv.content -> 'lines' ELSE '[]'::jsonb END"
)

_WANT_UNITS = _stmt(
    "SELECT jsonb_typeof(content -> 'lines') AS lines_kind,",
    "  COALESCE((",
    "    SELECT SUM((l ->> 'quantity')::bigint)",
    "    FROM jsonb_array_elements(",
    _LINES_OF_CONTENT,
    "    ) AS l",
    "    WHERE l ->> 'sku' = :scarcity_key",
    "      AND jsonb_typeof(l -> 'quantity') = 'number'",
    "  ), 0) AS want",
    "FROM checkout_versions",
    "WHERE checkout_id = :checkout_id AND version = :checkout_version",
)

_HELD_UNITS = _stmt(
    "SELECT COALESCE(SUM((line ->> 'quantity')::bigint), 0) AS held",
    "FROM reservations r",
    "JOIN checkout_versions cv",
    "  ON cv.tenant_id = r.tenant_id",
    " AND cv.checkout_id = r.checkout_id",
    " AND cv.version = r.checkout_version",
    "CROSS JOIN LATERAL jsonb_array_elements(",
    _LINES_OF_CV,
    ") AS line",
    "WHERE r.status = 'ACTIVE'",
    "  AND r.expires_at > now()",
    "  AND NOT (r.checkout_id = :checkout_id AND r.checkout_version = :checkout_version)",
    "  AND line ->> 'sku' = :scarcity_key",
    "  AND jsonb_typeof(line -> 'quantity') = 'number'",
    # Only positive quantities may contribute. Without this, a live hold whose content
    # carries a negative quantity for this item SUBTRACTS from the held total and can
    # cancel out a genuine hold, admitting an oversell — the one direction this query
    # must never fail in. `_units_wanted` already refuses a non-positive want; this is
    # the same guard on the read side.
    "  AND (line ->> 'quantity')::numeric > 0",
)

# Live holds whose checkout content cannot be inspected. Such a row might be holding the
# scarce item, and there is no way to prove it is not, so the capacity check must refuse
# rather than count it as zero.
_OPAQUE_HOLDS = text(
    "SELECT count(*) AS opaque "
    "FROM reservations r "
    "LEFT JOIN checkout_versions cv "
    "  ON cv.tenant_id = r.tenant_id "
    " AND cv.checkout_id = r.checkout_id "
    " AND cv.version = r.checkout_version "
    "WHERE r.status = 'ACTIVE' "
    "  AND r.expires_at > now() "
    "  AND NOT (r.checkout_id = :checkout_id AND r.checkout_version = :checkout_version) "
    "  AND (cv.id IS NULL OR jsonb_typeof(cv.content -> 'lines') IS DISTINCT FROM 'array')"
)


# ----------------------------------------------------------------------- internals


def _view(row: Row[Any]) -> ReservationView:
    """Materialise one projected row. All time facts on it came from the database."""
    return ReservationView(
        reservation_id=row.id,
        checkout_id=row.checkout_id,
        checkout_version=int(row.checkout_version),
        status=ReservationStatus(row.status),
        expires_at=row.expires_at,
        expired=bool(row.expired),
        seconds_remaining=int(row.seconds_remaining),
    )


def _current(
    session: Session, checkout_id: uuid.UUID, checkout_version: int, *, lock: bool
) -> ReservationView | None:
    stmt = _SELECT_CURRENT_FOR_UPDATE if lock else _SELECT_CURRENT
    row = session.execute(
        stmt, {"checkout_id": checkout_id, "checkout_version": checkout_version}
    ).one_or_none()
    return None if row is None else _view(row)


def _classify(view: ReservationView | None) -> RecoveryCode:
    """Map an observed reservation to the code admission should act on.

    A missing row and a lapsed row collapse to the same answer on purpose. From the
    caller's side both mean "there is no hold to spend", and both have the same remedy:
    re-reserve and re-quote. The closed enum in ``recovery`` has no NOT_FOUND, and
    inventing one here would put an untranslated code in front of an agent.
    """
    if view is None:
        return RecoveryCode.RESERVATION_EXPIRED
    if view.status is ReservationStatus.CONSUMED:
        # The hold was already spent by an admitted operation. Whether that was this
        # caller's own earlier request is decided by the kernel's idempotency record,
        # not here; this module only reports that the hold is no longer spendable.
        return RecoveryCode.DUPLICATE_OPERATION
    if view.status is not ReservationStatus.ACTIVE or view.expired:
        return RecoveryCode.RESERVATION_EXPIRED
    return RecoveryCode.OK


def _lock_scarcity(session: Session, tenant_id: uuid.UUID, scarcity_key: str) -> None:
    session.execute(_ADVISORY_XACT_LOCK, {"lock_key": f"scarcity:{tenant_id}:{scarcity_key}"})


def _lock_checkout_version(
    session: Session, tenant_id: uuid.UUID, checkout_id: uuid.UUID, checkout_version: int
) -> None:
    session.execute(
        _ADVISORY_XACT_LOCK,
        {"lock_key": f"reservation:{tenant_id}:{checkout_id}:{checkout_version}"},
    )


def _units_wanted(session: Session, checkout_id: uuid.UUID, version: int, key: str) -> int:
    """Units of ``key`` the checkout version itself asks for. Refuses to guess."""
    row = session.execute(
        _WANT_UNITS,
        {"checkout_id": checkout_id, "checkout_version": version, "scarcity_key": key},
    ).one_or_none()
    if row is None:
        raise ReservationContractError(
            f"no checkout version {checkout_id}/{version} is visible in this transaction; "
            "a reservation cannot be bound to a version that does not exist"
        )
    if row.lines_kind != "array":
        raise ReservationContractError(
            f"checkout version {checkout_id}/{version} has no 'lines' array in its content, "
            "so the units it holds of a scarce item cannot be determined"
        )
    want = int(row.want)
    if want <= 0:
        raise ReservationContractError(
            f"checkout version {checkout_id}/{version} contains no line for {key!r}; "
            "refusing to reserve stock the buyer's checkout does not name"
        )
    return want


def _units_held_elsewhere(session: Session, checkout_id: uuid.UUID, version: int, key: str) -> int:
    """Units of ``key`` withheld by other live reservations, per the database clock.

    Only ACTIVE rows that have not lapsed count. Rows a cleanup worker has not yet
    retired are excluded by ``expires_at > now()``, not by their status, so a worker
    outage cannot make stock look scarcer than it is either.
    """
    params: dict[str, Any] = {
        "checkout_id": checkout_id,
        "checkout_version": version,
        "scarcity_key": key,
    }
    opaque = session.execute(_OPAQUE_HOLDS, params).scalar_one()
    if int(opaque):
        raise ReservationContractError(
            f"{int(opaque)} live reservation(s) reference checkout content this module "
            "cannot read; refusing to compute a capacity check that would treat them as "
            "holding nothing"
        )
    return int(session.execute(_HELD_UNITS, params).scalar_one())


# ------------------------------------------------------------------------- public


def reserve(
    session: Session,
    *,
    checkout_id: uuid.UUID,
    checkout_version: int,
    ttl_seconds: int,
    allocations: Sequence[Allocation] = (),
) -> ReservationOutcome:
    """Take a hold on one immutable checkout version, expiring on the database clock.

    Guarantees:

    * ``expires_at`` is ``now() + ttl_seconds`` evaluated by PostgreSQL, so every pod
      agrees on when the hold ends regardless of its own clock.
    * At most one live hold exists per checkout version. Concurrent callers for the same
      version are serialized on a transaction-scoped advisory lock; the loser is told the
      hold already exists rather than creating a second one.
    * Every allocation in ``allocations`` is checked. Concurrent callers for any of those
      scarce items are serialized, and for each item the sum of live holds plus this one
      can never exceed its ``available_units``. Two agents racing for the last unit
      cannot both succeed.
    * The hold is created only if *all* allocations pass, so a basket cannot end up
      holding one scarce item while overselling another.

    Refuses:

    * ``DUPLICATE_OPERATION`` when a live hold for this version already exists; the
      existing hold is returned, and no second hold is created.
    * ``STALE_CHECKOUT`` when the hold was already consumed (this version is spent and a
      new version is needed), or when stock alone cannot cover the request.
    * ``CONCURRENT_OPERATION`` when stock could have covered the request but other live
      holds have taken it.

    Raises ``ReservationError`` for an unusable TTL or a malformed allocation list, and
    ``ReservationContractError`` when checkout content cannot support a capacity check;
    both abort the transaction, which is the fail-closed direction for an oversell guard.

    Call this at most once per transaction. Every scarce item a checkout needs belongs in
    one call: two calls in one transaction can each hold a lock the other wants.
    """
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ReservationError("ttl_seconds must be an int number of seconds")
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ReservationError(
            f"ttl_seconds must be in 1..{MAX_TTL_SECONDS}; got {ttl_seconds}. "
            "An unbounded hold withholds stock from every other buyer."
        )
    # Sorted by key, and duplicates refused, so that two transactions holding overlapping
    # baskets always take the same locks in the same order and cannot deadlock. Duplicates
    # would also let one item be checked twice against the same stock figure.
    wanted = sorted(allocations, key=lambda a: a.scarcity_key)
    keys = [a.scarcity_key for a in wanted]
    if len(set(keys)) != len(keys):
        raise ReservationError(f"allocations must name each scarce item once; got {keys}")

    tenant_id = require_tenant(session)

    # Fixed lock order — every scarcity key, then the checkout version — so that two
    # reservers can never each hold the lock the other is waiting for.
    for allocation in wanted:
        _lock_scarcity(session, tenant_id, allocation.scarcity_key)
    _lock_checkout_version(session, tenant_id, checkout_id, checkout_version)

    existing = _current(session, checkout_id, checkout_version, lock=True)
    if existing is not None:
        if existing.status is ReservationStatus.ACTIVE and not existing.expired:
            # Idempotent: the caller asked for a hold that already exists. Re-running the
            # capacity check here could refuse a hold this system has already granted.
            return ReservationOutcome(RecoveryCode.DUPLICATE_OPERATION, existing)
        if existing.status is ReservationStatus.CONSUMED:
            # Version N has been admitted. Re-holding it would let the same stock back a
            # second payment; the remedy is a new version, not a new hold.
            return ReservationOutcome(RecoveryCode.STALE_CHECKOUT, existing)
        # RELEASED, EXPIRED, or ACTIVE-but-lapsed: nothing is held, so re-reserving is
        # the documented recovery for RESERVATION_EXPIRED. It still goes through the
        # capacity check below.

    # Every allocation must pass before any row is written: a basket that holds one
    # scarce item while overselling another is still an oversell.
    for allocation in wanted:
        key = allocation.scarcity_key
        want = _units_wanted(session, checkout_id, checkout_version, key)
        if want > allocation.available_units:
            # Not a race: the item itself cannot cover this checkout. Re-quote.
            return ReservationOutcome(RecoveryCode.STALE_CHECKOUT, None)
        held = _units_held_elsewhere(session, checkout_id, checkout_version, key)
        if held + want > allocation.available_units:
            # Stock could have covered it; other live holds took it first. Retryable
            # once one of those holds is released or lapses.
            return ReservationOutcome(RecoveryCode.CONCURRENT_OPERATION, None)

    row = session.execute(
        _INSERT,
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "checkout_id": checkout_id,
            "checkout_version": checkout_version,
            "ttl_seconds": ttl_seconds,
        },
    ).one()
    return ReservationOutcome(RecoveryCode.OK, _view(row))


def check_validity(
    session: Session,
    *,
    checkout_id: uuid.UUID,
    checkout_version: int,
    lock: bool = True,
) -> ReservationOutcome:
    """Decide, on the database clock, whether a spendable hold exists right now.

    This is admission step 7 of specification 10.3. Pass ``lock=False`` only for display
    (a countdown, a support view); admission must hold the row for the rest of its
    transaction so the answer cannot change underneath it.

    Guarantees ``OK`` only for a row that is ``ACTIVE`` and whose ``expires_at`` is
    strictly in the future *according to PostgreSQL*. Refuses with
    ``RESERVATION_EXPIRED`` when the hold has lapsed, was released, was swept, or never
    existed — including when the row is still labelled ``ACTIVE`` because no cleanup
    worker has run. Returns ``DUPLICATE_OPERATION`` when the hold was already consumed.

    Raises ``TenantContextError`` when no tenant is bound: without it row-level security
    hides every row, and silence would be reported as expiry.
    """
    require_tenant(session)
    view = _current(session, checkout_id, checkout_version, lock=lock)
    return ReservationOutcome(_classify(view), view)


def consume(
    session: Session, *, checkout_id: uuid.UUID, checkout_version: int
) -> ReservationOutcome:
    """Spend the hold as part of a successful admission. Exactly one caller may win.

    The row is locked, then updated under ``status = 'ACTIVE' AND expires_at > now()``.
    Two admissions racing on the same checkout therefore produce one ``CONSUMED``
    transition and one refusal, decided by PostgreSQL rather than by ordering luck.

    Refuses with ``RESERVATION_EXPIRED`` if the hold lapsed on the database clock — even
    by a microsecond, and even if the caller's own clock disagrees — and with
    ``CONCURRENT_OPERATION`` if another admission consumed it first. A refusal means the
    caller must not go on to create a payment attempt.
    """
    require_tenant(session)
    view = _current(session, checkout_id, checkout_version, lock=True)
    code = _classify(view)
    if code is not RecoveryCode.OK or view is None:
        # A hold already consumed is not this caller's to spend again.
        if code is RecoveryCode.DUPLICATE_OPERATION:
            return ReservationOutcome(RecoveryCode.CONCURRENT_OPERATION, view)
        return ReservationOutcome(code, view)

    row = session.execute(_CONSUME, {"id": view.reservation_id}).one_or_none()
    if row is None:
        # The lock was granted only after a competing transaction committed; re-read to
        # report what actually happened rather than guessing.
        after = _current(session, checkout_id, checkout_version, lock=False)
        if after is not None and after.status is ReservationStatus.CONSUMED:
            return ReservationOutcome(RecoveryCode.CONCURRENT_OPERATION, after)
        return ReservationOutcome(RecoveryCode.RESERVATION_EXPIRED, after)
    return ReservationOutcome(RecoveryCode.OK, _view(row))


def release(
    session: Session,
    *,
    checkout_id: uuid.UUID,
    checkout_version: int,
    cause: ReleaseCause,
) -> ReservationOutcome:
    """End a hold — but only for a cause that is a confirmed fact.

    Guarantees:

    * ``PAYMENT_FAILED`` and ``CANCELLED`` release the hold from ``ACTIVE`` or
      ``CONSUMED``. A hold consumed at admission is still holding stock, so a confirmed
      failure afterwards must release it.
    * ``EXPIRED`` retires a hold only if PostgreSQL agrees it has lapsed. A pod running
      fast cannot retire a hold a buyer still legitimately has.

    Refuses:

    * ``PAYMENT_UNKNOWN`` / ``RECONCILING`` never release. The outcome may yet resolve to
      a completed payment, and reselling stock that was in fact paid for leaves the buyer
      charged for something the merchant no longer has. The hold is kept and the caller
      is told the outcome is unresolved (specification 10.4, 6.4.1).
    * ``RESERVATION_EXPIRED`` when the requested expiry has not actually happened.
    * ``DUPLICATE_OPERATION`` when the hold is already terminal, so a retried release is
      harmless.
    """
    require_tenant(session)
    view = _current(session, checkout_id, checkout_version, lock=True)

    if cause in HOLDING_CAUSES:
        # Deliberately evaluated before anything else: no branch below can release a hold
        # under an unresolved outcome, whatever state the row is in.
        return ReservationOutcome(_HOLDING_CODE[cause], view)
    if cause not in RELEASING_CAUSES:  # pragma: no cover - the enum is closed
        raise ReservationError(f"unhandled release cause {cause!r}")

    if view is None:
        return ReservationOutcome(RecoveryCode.RESERVATION_EXPIRED, None)
    if not view.holds_stock:
        return ReservationOutcome(RecoveryCode.DUPLICATE_OPERATION, view)

    if cause is ReleaseCause.EXPIRED:
        row = session.execute(_EXPIRE_ONE, {"id": view.reservation_id}).one_or_none()
        if row is None:
            # Either the hold is still live on the database clock, or another
            # transaction moved it first. Neither justifies forcing it to EXPIRED.
            return ReservationOutcome(
                RecoveryCode.CONCURRENT_OPERATION
                if view.expired
                else RecoveryCode.RESERVATION_EXPIRED,
                _current(session, checkout_id, checkout_version, lock=False),
            )
        return ReservationOutcome(RecoveryCode.OK, _view(row))

    row = session.execute(_RELEASE, {"id": view.reservation_id}).one_or_none()
    if row is None:  # pragma: no cover - requires losing the row lock we hold
        return ReservationOutcome(
            RecoveryCode.CONCURRENT_OPERATION,
            _current(session, checkout_id, checkout_version, lock=False),
        )
    return ReservationOutcome(RecoveryCode.OK, _view(row))


def sweep_expired(session: Session, *, limit: int = 500) -> int:
    """Retire lapsed holds. Hygiene only — never a precondition for correctness.

    Returns the number of rows moved to ``EXPIRED``. Every row it touches was already
    being refused at admission by ``check_validity`` and already counted as holding
    nothing by the capacity check, so the platform is correct whether or not this ever
    runs. What it buys is a table whose ``status`` column matches reality for operators
    and reporting.

    Uses ``FOR UPDATE SKIP LOCKED`` so several workers can sweep concurrently without
    blocking each other or an admission transaction holding the same row.
    """
    require_tenant(session)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ReservationError("limit must be a positive int batch size")
    return len(session.execute(_SWEEP, {"limit": limit}).fetchall())
