"""Delegated authority and the revocation epoch, specification 10.2 and 12.

A delegated authority is a bounded, revocable licence to move a buyer's money without
asking again: a Reserve Pay block, a single-use mandate. It is the one place in this
platform where a machine may act on money in the buyer's absence, so every bound it
carries -- merchant, buyer, currency, cumulative capacity, expiry, revocation epoch --
is re-verified inside the admitting transaction against a locked row, and never against
anything an agent, a transcript or a tool result asserted.

Three rules hold everywhere in this module.

**One lock, one linearization point.** :func:`revoke` and :func:`check_authority` both
take ``SELECT ... FOR UPDATE`` on the same authority row. That shared lock is what turns
"revoked before admission" into a total order rather than a race: whichever transaction
commits first is the one the other is forced to observe. Without it, a revocation and an
admission read the same epoch concurrently and both proceed, and the buyer is debited
after cancelling -- the exact failure the epoch exists to prevent.

**The clock belongs to the database.** No function here reads an application clock.
Expiry is evaluated as ``expires_at <= now()`` inside the same statement that takes the
lock, so a pod whose system time has drifted forward cannot admit against an expired
authority, and one drifted backward cannot resurrect one. The snapshot returned to
callers carries the database's verdict (:attr:`AuthoritySnapshot.expired`) rather than a
timestamp for the caller to re-compare locally.

**Epochs only ever go up.** The epoch column is written in exactly one statement, in
:func:`revoke`, as ``revocation_epoch + 1`` evaluated by the database. No public function
in this module accepts an epoch to store. An approval recorded under epoch N is therefore
permanently invalid the moment any revocation lands, and that invalidity is a numeric
comparison rather than a status anyone has to remember to update.

Capacity is defended twice on purpose: this module refuses an over-budget debit before
issuing the UPDATE, the UPDATE itself re-tests the budget in its WHERE clause, and the
``ck_delegated_authorities_consumed_within_max`` CHECK constraint would abort the
transaction if both were somehow wrong. Money that leaks past two application checks
still cannot be written down.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from commerce_domain import Money, RecoveryCode, uuid7
from sqlalchemy import Row, text
from sqlalchemy.orm import Session

# --------------------------------------------------------------------------- errors


class AuthorityError(RuntimeError):
    """A caller-side misuse of this module. Never a buyer-visible outcome.

    Distinct from a denial: a denial is an expected answer carried by a
    :class:`~commerce_domain.recovery.RecoveryCode`, while this is a bug in the
    calling code that must not be translated into conversation and retried.
    """


class UnknownAuthorityError(AuthorityError):
    """The named authority is not visible in this transaction's tenant.

    Raised by :func:`revoke` rather than returned, because a revocation that quietly
    reports success against a row it never found leaves the buyer exposed while the
    operator believes the authority is dead.
    """


# --------------------------------------------------------------------------- vocabulary


class AuthorityKind(StrEnum):
    """How much of the authority one admitted debit consumes."""

    SINGLE_USE = "SINGLE_USE"
    RESERVE = "RESERVE"


class AuthorityStatus(StrEnum):
    """Platform-normalized authority state, specification 12.2.

    These are internal normalization states; they are not claimed Razorpay or NPCI
    enum names.
    """

    ACTIVE = "ACTIVE"
    EXHAUSTED = "EXHAUSTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    RECONCILING = "RECONCILING"


class AuthorityReason(StrEnum):
    """Stable reason key accompanying a decision, never a sentence.

    An agent renders one of these into the buyer's language; a template renders it into
    speech. Neither may alter the :class:`RecoveryCode` it travels with. Keys are closed
    and reviewed here so that two agents cannot disagree about what a refusal meant.
    """

    OK = "OK"
    AUTHORITY_NOT_FOUND = "AUTHORITY_NOT_FOUND"
    AUTHORITY_EPOCH_STALE = "AUTHORITY_EPOCH_STALE"
    AUTHORITY_EPOCH_UNKNOWN = "AUTHORITY_EPOCH_UNKNOWN"
    AUTHORITY_REVOKED = "AUTHORITY_REVOKED"
    AUTHORITY_EXPIRED = "AUTHORITY_EXPIRED"
    AUTHORITY_EXHAUSTED = "AUTHORITY_EXHAUSTED"
    AUTHORITY_RECONCILING = "AUTHORITY_RECONCILING"
    AUTHORITY_NOT_ACTIVE = "AUTHORITY_NOT_ACTIVE"
    MERCHANT_OUT_OF_SCOPE = "MERCHANT_OUT_OF_SCOPE"
    BUYER_OUT_OF_SCOPE = "BUYER_OUT_OF_SCOPE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    SINGLE_USE_ALREADY_CONSUMED = "SINGLE_USE_ALREADY_CONSUMED"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"


# --------------------------------------------------------------------------- values


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    """One authority row as it stood under the lock, plus the database's own clock.

    ``expired`` is computed by PostgreSQL in the locking statement. It is carried here
    rather than left to the caller so that no downstream code can re-decide expiry with
    ``datetime.now()`` on a skewed pod.
    """

    authority_id: uuid.UUID
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    buyer_ref: str
    kind: AuthorityKind
    status: AuthorityStatus
    revocation_epoch: int
    currency: str
    max_amount: Money
    consumed_amount: Money
    expires_at: datetime
    expired: bool
    observed_at: datetime

    @property
    def remaining(self) -> Money:
        """Cumulative capacity still available. Never negative while the CHECK holds."""
        return self.max_amount - self.consumed_amount


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """The answer to "may this debit be admitted against this authority?".

    Always structured. ``code`` carries the decision, ``reason`` carries the stable
    explanation key, and neither is a free-text string an agent may reinterpret.
    """

    code: RecoveryCode
    reason: AuthorityReason
    authority_id: uuid.UUID
    requested: Money
    snapshot: AuthoritySnapshot | None = None

    def __post_init__(self) -> None:
        # An allowed decision without the row it was decided against cannot be audited
        # afterwards, and an OK code paired with a denial reason would let a caller
        # branch on the wrong field.
        if self.code is RecoveryCode.OK:
            if self.reason is not AuthorityReason.OK:
                raise AuthorityError(f"OK decision must carry reason OK, got {self.reason}")
            if self.snapshot is None:
                raise AuthorityError("an allowed decision must name the authority row it used")
        elif self.reason is AuthorityReason.OK:
            raise AuthorityError(f"denied decision cannot carry reason OK, code was {self.code}")

    @property
    def allowed(self) -> bool:
        return self.code is RecoveryCode.OK

    @property
    def observed_epoch(self) -> int | None:
        """Epoch seen under the lock, or None when no row was visible."""
        return None if self.snapshot is None else self.snapshot.revocation_epoch


@dataclass(frozen=True, slots=True)
class RevocationOutcome:
    """Result of a revocation. ``epoch`` is the new, higher epoch."""

    authority_id: uuid.UUID
    previous_epoch: int
    epoch: int
    previous_status: AuthorityStatus

    @property
    def was_already_revoked(self) -> bool:
        """True when the authority was revoked before this call.

        A repeat revocation still raises the epoch. That is deliberate: the epoch is the
        invalidation mechanism, and refusing to move it on a retry would make a
        re-revocation after any capacity restoration silently ineffective.
        """
        return self.previous_status is AuthorityStatus.REVOKED


# --------------------------------------------------------------------------- statements

# The locking read. Every gate in this module is decided from this one row version, so
# nothing can change underneath a partially evaluated decision. `now()` is transaction
# start time, which gives every check inside one admission transaction the same instant
# to compare against.
_LOCK_AUTHORITY: Final = text(
    """
    SELECT id,
           tenant_id,
           merchant_id,
           buyer_ref,
           kind,
           status,
           revocation_epoch,
           currency,
           max_amount_minor,
           consumed_amount_minor,
           expires_at,
           (expires_at <= now()) AS expired,
           now() AS observed_at
      FROM delegated_authorities
     WHERE id = :authority_id
       FOR UPDATE
    """
)

# `revocation_epoch + 1` is evaluated by the database against the locked row, so the new
# epoch cannot be supplied, replayed or lowered by a caller. The epoch guard in WHERE is
# redundant while the lock is held and is kept as the assertion that it was.
_REVOKE_AUTHORITY: Final = text(
    """
    UPDATE delegated_authorities
       SET revocation_epoch = revocation_epoch + 1,
           status = 'REVOKED'
     WHERE id = :authority_id
       AND revocation_epoch = :observed_epoch
    RETURNING revocation_epoch
    """
)

# Capacity, epoch, status and expiry are all re-tested here in the WHERE clause. The
# lock already guarantees they cannot have moved; this is the statement that stays
# correct if a future caller forgets to hold it.
_ADMIT_DEBIT: Final = text(
    """
    UPDATE delegated_authorities
       SET consumed_amount_minor = consumed_amount_minor + :amount_minor,
           status = CASE
                      WHEN kind = 'SINGLE_USE'
                        OR consumed_amount_minor + :amount_minor >= max_amount_minor
                      THEN 'EXHAUSTED'
                      ELSE status
                    END
     WHERE id = :authority_id
       AND revocation_epoch = :expected_epoch
       AND status = 'ACTIVE'
       AND expires_at > now()
       AND consumed_amount_minor + :amount_minor <= max_amount_minor
    RETURNING consumed_amount_minor, status, revocation_epoch
    """
)

# INSERT ... SELECT ... WHERE so that the future-expiry test is made by the database
# clock in the same statement that stores the row. An authority born already expired
# would be admitted by nothing but would look live to an operator reading the table.
_GRANT_AUTHORITY: Final = text(
    """
    INSERT INTO delegated_authorities
        (id, tenant_id, merchant_id, buyer_ref, kind, status, revocation_epoch,
         currency, max_amount_minor, consumed_amount_minor, expires_at)
    SELECT :authority_id, :tenant_id, :merchant_id, :buyer_ref, :kind, 'ACTIVE', 0,
           :currency, :max_amount_minor, 0, e.expires_at
      FROM (
        SELECT COALESCE(
                 CAST(:expires_at AS timestamptz),
                 now() + CAST(:ttl_seconds AS double precision) * interval '1 second'
               ) AS expires_at
      ) AS e
     WHERE e.expires_at > now()
    RETURNING id
    """
)


# --------------------------------------------------------------------------- helpers


def _require_transaction(session: Session) -> None:
    """Refuse to work outside an explicit transaction.

    ``SELECT ... FOR UPDATE`` holds its lock only until the transaction ends. If the
    caller has not opened one, the lock this module takes is released at a moment the
    caller never reasoned about, and the linearization point silently disappears.
    """
    if not session.in_transaction():
        raise AuthorityError(
            "authority operations require an active transaction: the row lock is the "
            "linearization point between revocation and admission and is only held for "
            "the life of the caller's transaction"
        )


def _snapshot(row: Row[Any]) -> AuthoritySnapshot:
    """Coerce one locked row into the immutable snapshot the gates are decided from."""
    currency = str(row.currency)
    return AuthoritySnapshot(
        authority_id=uuid.UUID(str(row.id)),
        tenant_id=uuid.UUID(str(row.tenant_id)),
        merchant_id=uuid.UUID(str(row.merchant_id)),
        buyer_ref=str(row.buyer_ref),
        kind=AuthorityKind(str(row.kind)),
        status=AuthorityStatus(str(row.status)),
        revocation_epoch=int(row.revocation_epoch),
        currency=currency,
        max_amount=Money(int(row.max_amount_minor), currency),
        consumed_amount=Money(int(row.consumed_amount_minor), currency),
        expires_at=row.expires_at,
        expired=bool(row.expired),
        observed_at=row.observed_at,
    )


def _deny(
    code: RecoveryCode,
    reason: AuthorityReason,
    authority_id: uuid.UUID,
    requested: Money,
    snapshot: AuthoritySnapshot | None,
) -> AuthorityDecision:
    return AuthorityDecision(
        code=code,
        reason=reason,
        authority_id=authority_id,
        requested=requested,
        snapshot=snapshot,
    )


# --------------------------------------------------------------------------- public API


def lock_authority(session: Session, authority_id: uuid.UUID) -> AuthoritySnapshot | None:
    """Take the row lock and return the authority as the database sees it right now.

    Guarantees that on return the calling transaction holds an exclusive row lock on this
    authority, so no concurrent revocation or debit can interleave until it commits or
    rolls back. Returns None when no such row is visible -- which, under row-level
    security, also covers an authority belonging to another tenant and a transaction with
    no tenant bound at all. Both are absences, and both must fail closed.

    Refuses to run outside an explicit transaction.
    """
    _require_transaction(session)
    row = session.execute(_LOCK_AUTHORITY, {"authority_id": authority_id}).one_or_none()
    return None if row is None else _snapshot(row)


def revoke(session: Session, authority_id: uuid.UUID) -> RevocationOutcome:
    """Revoke an authority by raising its revocation epoch under the row lock.

    Guarantees the new epoch is exactly one greater than the epoch stored when the lock
    was taken, computed by the database, and that the row is left ``REVOKED``. Every
    approval that recorded the old epoch becomes invalid at the instant this commits,
    with no further writes and nothing to remember to update.

    Concurrency: this takes the same lock :func:`check_authority` takes. If an admission
    is in flight it either commits before this call acquires the lock -- in which case
    that one debit stands and every later one is blocked -- or it waits, sees the raised
    epoch and is refused. There is no interleaving in which both proceed.

    Refuses an authority not visible in this transaction's tenant, by raising
    :class:`UnknownAuthorityError`: a revocation that reports success without touching a
    row would leave the buyer exposed while the operator believes it is dead.
    """
    _require_transaction(session)
    snapshot = lock_authority(session, authority_id)
    if snapshot is None:
        raise UnknownAuthorityError(
            f"authority {authority_id} is not visible in this transaction; "
            "refusing to report a revocation that touched no row"
        )

    new_epoch = session.execute(
        _REVOKE_AUTHORITY,
        {"authority_id": authority_id, "observed_epoch": snapshot.revocation_epoch},
    ).scalar_one_or_none()
    if new_epoch is None:
        # Unreachable while the lock above is held; if it ever fires, the lock was lost
        # and every guarantee in this module is void, so fail loudly rather than report
        # a revocation that did not happen.
        raise AuthorityError(
            f"authority {authority_id} changed while its row lock was held; "
            "revocation could not be linearized"
        )

    epoch = int(new_epoch)
    if epoch <= snapshot.revocation_epoch:
        raise AuthorityError(
            f"revocation epoch did not increase ({snapshot.revocation_epoch} -> {epoch})"
        )
    return RevocationOutcome(
        authority_id=authority_id,
        previous_epoch=snapshot.revocation_epoch,
        epoch=epoch,
        previous_status=snapshot.status,
    )


def check_authority(
    session: Session,
    authority_id: uuid.UUID,
    *,
    expected_epoch: int,
    amount: Money,
    merchant_id: uuid.UUID,
    buyer_ref: str | None = None,
) -> AuthorityDecision:
    """Decide whether ``amount`` may be admitted against this authority, under its lock.

    Guarantees, on return, that the calling transaction holds the authority's row lock,
    so the decision stays true until the caller commits or rolls back. Everything is
    re-derived from the locked row: nothing an agent supplied is trusted except the
    identifiers being checked.

    Refuses, in this order:

    * no visible row -> ``AUTHORITY_INSUFFICIENT`` / ``AUTHORITY_NOT_FOUND``. Absence
      covers another tenant's row and an unbound tenant context; both fail closed.
    * ``expected_epoch`` below the stored epoch -> ``AUTHORITY_REVOKED``. This is the
      case where an approval was recorded under epoch N and a revocation has since
      landed.
    * ``expected_epoch`` above the stored epoch -> ``AUTHORITY_REVOKED``. The caller is
      quoting an epoch this row never reached; treat a forged or corrupted proof as
      revoked rather than guessing.
    * status ``REVOKED`` -> ``AUTHORITY_REVOKED``.
    * expired by the **database** clock, or status ``EXPIRED`` ->
      ``AUTHORITY_INSUFFICIENT`` / ``AUTHORITY_EXPIRED``. Expiry is a scope bound, not a
      revocation, so it takes the scope code.
    * status ``RECONCILING`` -> ``RECONCILIATION_IN_PROGRESS``. Remaining capacity is
      unknown until the provider outcome is settled, and spending against an unknown
      balance is how a double debit happens.
    * status ``EXHAUSTED``, or any other non-``ACTIVE`` status, a different merchant, a
      different buyer, a different currency, a single-use authority already consumed, or
      a debit exceeding remaining cumulative capacity -> ``AUTHORITY_INSUFFICIENT``.

    Raises :class:`AuthorityError` for caller bugs -- a non-positive amount, a negative
    epoch, no active transaction -- because those are never a buyer-facing outcome.
    """
    _require_transaction(session)
    if expected_epoch < 0:
        raise AuthorityError(f"expected_epoch must be non-negative, got {expected_epoch}")
    if amount.minor <= 0:
        # A zero debit consumes nothing and means nothing; a negative one would *restore*
        # cumulative capacity through the admission path, inflating what the buyer
        # authorized. Neither is a denial to explain, both are bugs to fix.
        raise AuthorityError(f"debit amount must be positive, got {amount}")

    snapshot = lock_authority(session, authority_id)
    if snapshot is None:
        return _deny(
            RecoveryCode.AUTHORITY_INSUFFICIENT,
            AuthorityReason.AUTHORITY_NOT_FOUND,
            authority_id,
            amount,
            None,
        )

    def denied(code: RecoveryCode, reason: AuthorityReason) -> AuthorityDecision:
        return _deny(code, reason, authority_id, amount, snapshot)

    # Epoch first: revocation outranks every other bound, and specification 10.2 makes
    # a raised epoch the signal that blocks all subsequent actions.
    if expected_epoch < snapshot.revocation_epoch:
        return denied(RecoveryCode.AUTHORITY_REVOKED, AuthorityReason.AUTHORITY_EPOCH_STALE)
    if expected_epoch > snapshot.revocation_epoch:
        return denied(RecoveryCode.AUTHORITY_REVOKED, AuthorityReason.AUTHORITY_EPOCH_UNKNOWN)
    if snapshot.status is AuthorityStatus.REVOKED:
        return denied(RecoveryCode.AUTHORITY_REVOKED, AuthorityReason.AUTHORITY_REVOKED)

    # `expired` was decided by PostgreSQL in the locking statement. It is never
    # recomputed here, so a pod with a skewed system clock has no way to disagree.
    if snapshot.expired or snapshot.status is AuthorityStatus.EXPIRED:
        return denied(RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.AUTHORITY_EXPIRED)
    if snapshot.status is AuthorityStatus.RECONCILING:
        return denied(
            RecoveryCode.RECONCILIATION_IN_PROGRESS, AuthorityReason.AUTHORITY_RECONCILING
        )
    if snapshot.status is AuthorityStatus.EXHAUSTED:
        return denied(RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.AUTHORITY_EXHAUSTED)
    if snapshot.status is not AuthorityStatus.ACTIVE:
        return denied(RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.AUTHORITY_NOT_ACTIVE)

    if snapshot.merchant_id != merchant_id:
        return denied(RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.MERCHANT_OUT_OF_SCOPE)
    if buyer_ref is not None and snapshot.buyer_ref != buyer_ref:
        return denied(RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.BUYER_OUT_OF_SCOPE)
    if snapshot.currency != amount.currency:
        # Compared before any arithmetic: Money refuses cross-currency operations, and a
        # currency mismatch is a scope failure to explain, not an exception to surface.
        return denied(RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.CURRENCY_MISMATCH)

    if snapshot.kind is AuthorityKind.SINGLE_USE and snapshot.consumed_amount.minor > 0:
        return denied(
            RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.SINGLE_USE_ALREADY_CONSUMED
        )
    if amount > snapshot.remaining:
        return denied(RecoveryCode.AUTHORITY_INSUFFICIENT, AuthorityReason.CAPACITY_EXCEEDED)

    return AuthorityDecision(
        code=RecoveryCode.OK,
        reason=AuthorityReason.OK,
        authority_id=authority_id,
        requested=amount,
        snapshot=snapshot,
    )


def admit_debit(
    session: Session,
    authority_id: uuid.UUID,
    *,
    expected_epoch: int,
    amount: Money,
    merchant_id: uuid.UUID,
    buyer_ref: str | None = None,
) -> AuthorityDecision:
    """Check the authority and, if it passes, allocate ``amount`` against its capacity.

    Guarantees that the capacity allocation happens in the same transaction and under the
    same row lock as the check that permitted it, so the sum of admitted, non-failed
    debits can never exceed ``max_amount_minor``. A ``SINGLE_USE`` authority, and a
    ``RESERVE`` whose capacity is now fully allocated, are left ``EXHAUSTED``.

    On success, ``snapshot`` reflects the row *after* allocation, so
    ``decision.snapshot.remaining`` is the capacity a subsequent debit may use.

    Refuses everything :func:`check_authority` refuses, returning the same decision
    unchanged and writing nothing. Returns ``CONCURRENT_OPERATION`` in the theoretically
    unreachable case where the guarded UPDATE matches no row while the lock is held: that
    means the lock was lost, and reporting a debit that did not happen would be worse
    than asking the caller to retry.

    This does not itself release capacity on a failed provider call. A debit whose
    outcome is unknown stays allocated until reconciliation settles it, because
    restoring capacity before the provider has confirmed the debit did not happen is how
    a buyer gets charged twice.
    """
    decision = check_authority(
        session,
        authority_id,
        expected_epoch=expected_epoch,
        amount=amount,
        merchant_id=merchant_id,
        buyer_ref=buyer_ref,
    )
    if not decision.allowed:
        return decision
    if decision.snapshot is None:  # pragma: no cover - AuthorityDecision refuses this shape
        raise AuthorityError("allowed decision arrived without the row it was decided from")

    row = session.execute(
        _ADMIT_DEBIT,
        {
            "authority_id": authority_id,
            "expected_epoch": expected_epoch,
            "amount_minor": amount.minor,
        },
    ).one_or_none()
    if row is None:
        return _deny(
            RecoveryCode.CONCURRENT_OPERATION,
            AuthorityReason.CONCURRENT_MODIFICATION,
            authority_id,
            amount,
            decision.snapshot,
        )

    allocated = replace(
        decision.snapshot,
        consumed_amount=Money(int(row.consumed_amount_minor), decision.snapshot.currency),
        status=AuthorityStatus(str(row.status)),
        revocation_epoch=int(row.revocation_epoch),
    )
    if allocated.remaining.is_negative:
        # Belt beyond the CHECK constraint: never hand back a snapshot claiming the
        # buyer authorized less than has been spent.
        raise AuthorityError(
            f"authority {authority_id} over-allocated: "
            f"{allocated.consumed_amount} consumed of {allocated.max_amount}"
        )
    return AuthorityDecision(
        code=RecoveryCode.OK,
        reason=AuthorityReason.OK,
        authority_id=authority_id,
        requested=amount,
        snapshot=allocated,
    )


def grant_authority(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    buyer_ref: str,
    kind: AuthorityKind,
    max_amount: Money,
    ttl_seconds: float | None = None,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    """Record a new delegated authority at epoch 0 with nothing consumed.

    Guarantees the row starts ``ACTIVE`` at ``revocation_epoch = 0`` with
    ``consumed_amount_minor = 0``, and that its expiry is in the future *by the database
    clock* -- checked in the same statement that inserts it, so a pod with a fast clock
    cannot create an authority that is already dead but looks live in the table.

    Exactly one of ``ttl_seconds`` (anchored to the database clock) or ``expires_at`` (an
    absolute, timezone-aware instant, as a provider mandate would supply) must be given.

    Refuses a non-positive maximum, a naive ``expires_at``, both or neither expiry form,
    and any expiry not strictly in the future. Does not accept an epoch: epochs are
    written only by :func:`revoke`.
    """
    _require_transaction(session)
    if (ttl_seconds is None) == (expires_at is None):
        raise AuthorityError("supply exactly one of ttl_seconds or expires_at")
    if ttl_seconds is not None and ttl_seconds <= 0:
        raise AuthorityError(f"ttl_seconds must be positive, got {ttl_seconds}")
    if expires_at is not None and expires_at.tzinfo is None:
        # A naive timestamp would be interpreted in the database session's time zone,
        # which is a different clock question than the one the caller thinks they asked.
        raise AuthorityError("expires_at must be timezone-aware")
    if max_amount.minor <= 0:
        raise AuthorityError(f"max_amount must be positive, got {max_amount}")

    authority_id: uuid.UUID = uuid7()
    created = session.execute(
        _GRANT_AUTHORITY,
        {
            "authority_id": authority_id,
            "tenant_id": tenant_id,
            "merchant_id": merchant_id,
            "buyer_ref": buyer_ref,
            "kind": str(kind),
            "currency": max_amount.currency,
            "max_amount_minor": max_amount.minor,
            "ttl_seconds": ttl_seconds,
            "expires_at": expires_at,
        },
    ).scalar_one_or_none()
    if created is None:
        raise AuthorityError(
            "refusing to create an authority whose expiry is not in the future "
            "by the database clock"
        )
    return authority_id
