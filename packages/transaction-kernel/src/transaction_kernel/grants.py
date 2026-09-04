"""Single-use Execution Grants, specification 10.3.1.

A grant is the only thing that authorizes one provider mutation. The kernel issues it
after a successful admission; the restricted worker locks it, verifies every bound field
against the command it is about to send, consumes it, and only then talks to the
provider. Nothing here calls a model, and no field of a grant is ever derived from
model output.

What this module guarantees
---------------------------
*Consumed at most once.* :func:`consume_grant` takes ``SELECT ... FOR UPDATE`` on the
grant row and transitions ``ISSUED -> CONSUMED`` under that lock. Two workers handed the
same delivery contend on the row; exactly one wins and the loser is told the operation is
already spoken for.

*Bound byte-for-byte.* A grant carries the tenant, checkout id, immutable version,
canonical content hash, payment attempt, operation, amount in minor units and currency
that were admitted. :func:`consume_grant` compares every one of them against what the
caller intends to send. An agent compromised after admission cannot move the amount, the
merchant or the checkout, because a changed command no longer matches the grant and the
grant is not consumed.

*Never a replacement after consumption.* Specification 10.3.1: a provider timeout after
consumption becomes ``UNKNOWN`` and is reconciled under the same operation. It never
produces a second grant and never a second charge. :func:`issue_grant` therefore refuses
to issue a second grant for a payment attempt whose grant for that operation was already
consumed. Recovery from an uncertain outcome is reconciliation, not re-issuance. A
legitimate retry after a *confirmed* failure creates a new payment attempt and binds its
grant to that new attempt id, so this refusal blocks the dangerous path only.

*The clock is the database's.* Every expiry comparison is ``now()`` evaluated by
PostgreSQL, never ``datetime.now()`` in a pod. A pod whose clock runs slow must not be
able to consume a grant that expired minutes ago, and one whose clock runs fast must not
be able to reject a live one.

Failure surface
---------------
A refusal that an agent or worker may have to act on raises a :class:`GrantError`
carrying a :class:`~transaction_kernel.recovery.RecoveryCode`. A caller that uses this
API wrongly -- a negative TTL, a zero amount -- gets :class:`ValueError`, because there is
no buyer-facing recovery from a kernel calling itself incorrectly and inventing a
recovery code for it would put a programming bug into the agent's vocabulary.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

from commerce_domain import Money, uuid7
from platform_db import ExecutionGrant, require_tenant
from sqlalchemy import CursorResult, Select, and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .contracts import CheckoutRef, Delta, Operation
from .recovery import RecoveryCode

__all__ = [
    "MAX_GRANT_TTL_SECONDS",
    "GrantAlreadyConsumedError",
    "GrantBinding",
    "GrantBindingMismatchError",
    "GrantError",
    "GrantExpiredError",
    "GrantNotFoundError",
    "GrantReplacementRefusedError",
    "GrantRevokedError",
    "GrantStatus",
    "GrantTenantMismatchError",
    "consume_grant",
    "expire_stale_grants",
    "issue_grant",
    "revoke_grant",
    "revoke_unused_grants",
]


class GrantStatus(StrEnum):
    """The four states in specification 10.3.1. Mirrors the ``status_enum`` check
    constraint on ``execution_grants``; a value not listed here cannot be stored."""

    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


#: Upper bound on a grant's lifetime. A grant is a capability to move money; the longer it
#: lives the more it resembles a bearer token, which is precisely what specification
#: 10.3.1 says it must not be. Fifteen minutes comfortably covers an outbox lease plus
#: provider round trip and nothing else.
MAX_GRANT_TTL_SECONDS: Final = 900

#: Name of the partial unique index that makes "one live grant per payment attempt" a
#: database guarantee. Matched by name so an unrelated unique violation is never
#: misreported as a grant conflict.
_ONE_ACTIVE_PER_ATTEMPT: Final = "uq_execution_grants_one_active_per_attempt"

#: What blocks a new grant, in the two different shapes the two rules actually have.
#:
#: ISSUED blocks every operation on the attempt, not merely a repeat of the same one,
#: because the partial unique index is on ``(tenant_id, payment_attempt_id)`` and does not
#: mention the operation. The pre-check has to be exactly this wide: narrower and the
#: index refuses later instead, which aborts the caller's transaction and reports it as a
#: race that never happened.
#:
#: CONSUMED blocks only its own operation. Specification 10.3.1 forbids a replacement for
#: an operation that was already started; it does not forbid refunding a payment.
_ISSUED_BLOCKS_ANY_OPERATION: Final = GrantStatus.ISSUED
_CONSUMED_BLOCKS_SAME_OPERATION: Final = GrantStatus.CONSUMED


# --------------------------------------------------------------------------- failures


class GrantError(Exception):
    """A deterministic refusal from the grant service.

    ``code`` is the structured outcome the caller must act on. Callers translate the code
    into language; they may not override it or substitute a reason of their own.
    """

    code: RecoveryCode = RecoveryCode.POLICY_EXCEPTION

    def __init__(self, message: str, *, grant_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.grant_id = grant_id


class GrantNotFoundError(GrantError):
    """No grant with this id is visible to the bound tenant.

    Row-level security makes another tenant's grant indistinguishable from a grant that
    never existed, which is deliberate: a probe cannot use this API to learn whether an
    id belongs to somebody else.
    """

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class GrantTenantMismatchError(GrantError):
    """The caller named a tenant other than the one bound to this transaction."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class GrantBindingMismatchError(GrantError):
    """The command the caller intends to send is not the command that was admitted."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT

    def __init__(
        self, message: str, *, deltas: tuple[Delta, ...], grant_id: uuid.UUID | None = None
    ) -> None:
        super().__init__(message, grant_id=grant_id)
        #: Every field that differed, not merely the first, so the audit record shows the
        #: full shape of the attempted substitution.
        self.deltas = deltas


class GrantAlreadyConsumedError(GrantError):
    """This grant's single use is already spent.

    The code is ``CONCURRENT_OPERATION`` and deliberately not ``DUPLICATE_OPERATION``.
    ``DUPLICATE_OPERATION`` is one of the two codes an agent may present to a buyer as a
    completed money action; a consumed grant proves only that the operation was *started*,
    never that the provider confirmed it. The second caller must read the authoritative
    payment state or reconcile -- never announce success and never send the command again.
    """

    code = RecoveryCode.CONCURRENT_OPERATION


class GrantReplacementRefusedError(GrantError):
    """A grant for this payment attempt and operation already exists.

    Issued: one live grant per attempt is the point. Consumed: specification 10.3.1
    forbids a replacement grant after consumption, because the provider may already hold
    the request and a second grant is a second charge waiting to happen.
    """

    code = RecoveryCode.CONCURRENT_OPERATION


class GrantExpiredError(GrantError):
    """The grant's window closed, by the database clock, before it was consumed.

    Nothing was sent to the provider, so the same logical operation may be re-admitted;
    that re-admission re-checks the reservation and the approval from scratch, which is
    what ``RESERVATION_EXPIRED`` instructs the caller to do.
    """

    code = RecoveryCode.RESERVATION_EXPIRED


class GrantRevokedError(GrantError):
    """The grant was withdrawn before use, for example by Safe Mode activation.

    The row does not record *why* it was revoked, so this returns the code that stops
    execution under every cause rather than guessing at one. An operator-visible reason
    lives in the Safe Mode record and the audit stream, not in the grant.
    """

    code = RecoveryCode.AUTHORITY_REVOKED


# --------------------------------------------------------------------------- binding


@dataclass(frozen=True, slots=True)
class GrantBinding:
    """Exactly what the caller is about to ask the provider to do.

    Built from the command the worker holds -- never from the grant row -- and handed to
    :func:`consume_grant`, which refuses unless every field matches the admitted grant.
    There is deliberately no ``from_grant`` constructor: a binding derived from the row it
    is meant to check would always match and the verification would be theatre.
    """

    tenant_id: uuid.UUID
    checkout: CheckoutRef
    payment_attempt_id: uuid.UUID
    operation: Operation
    amount: Money


def _binding_deltas(grant: ExecutionGrant, expected: GrantBinding) -> tuple[Delta, ...]:
    """Every bound field that differs between the admitted grant and the intended command.

    Amount is compared as integer minor units beside its currency code. Nothing here
    converts, rounds or normalizes: 39500 INR and 39500 USD are two different mismatches,
    and 39500 is never equal to 395.
    """
    checks: tuple[tuple[str, object, object], ...] = (
        ("tenant_id", grant.tenant_id, expected.tenant_id),
        ("checkout_id", grant.checkout_id, expected.checkout.checkout_id),
        ("checkout_version", grant.checkout_version, expected.checkout.version),
        ("content_hash", grant.content_hash, expected.checkout.content_hash),
        ("payment_attempt_id", grant.payment_attempt_id, expected.payment_attempt_id),
        ("operation", grant.operation, expected.operation.value),
        ("amount_minor", grant.amount_minor, expected.amount.minor),
        ("currency", grant.currency, expected.amount.currency),
    )
    return tuple(
        Delta(field_path=name, approved=admitted, current=intended, reason="GRANT_BINDING")
        for name, admitted, intended in checks
        if admitted != intended
    )


# --------------------------------------------------------------------------- issuance


def issue_grant(
    session: Session,
    *,
    tenant: uuid.UUID,
    checkout_ref: CheckoutRef,
    payment_attempt_id: uuid.UUID,
    operation: Operation,
    amount: Money,
    kernel_decision_id: uuid.UUID,
    ttl_seconds: int,
) -> ExecutionGrant:
    """Issue one single-use grant for one admitted operation.

    Guarantees:

    * The row binds tenant, checkout id, immutable version, canonical content hash,
      payment attempt, operation, amount minor units, currency and the kernel decision
      that authorized it.
    * ``issued_at`` and ``expires_at`` are both computed by the database clock inside this
      transaction, so ``expires_at - issued_at`` is exactly ``ttl_seconds`` regardless of
      what any application pod believes the time to be.
    * At most one ``ISSUED`` grant exists per payment attempt. The database enforces this
      with a partial unique index; a concurrent second issue loses.

    Refuses (:class:`GrantReplacementRefusedError`):

    * A payment attempt that already holds an ``ISSUED`` grant, *whatever operation that
      grant names*. This is the database's rule, not this function's: the partial unique
      index keys on ``(tenant_id, payment_attempt_id)`` alone. A refund grant therefore
      cannot be minted while the payment grant on the same attempt is still live; it can
      once that grant is consumed, expired or revoked.
    * A payment attempt whose grant *for this same operation* was already ``CONSUMED``.
      This is specification 10.3.1's rule that an uncertain outcome is reconciled, never
      re-granted. A different operation on a consumed attempt -- a refund after a payment
      -- is allowed.

    Refuses otherwise:

    * A tenant other than the one bound to this transaction
      (:class:`GrantTenantMismatchError`).
    * A non-positive amount, a TTL outside ``1..MAX_GRANT_TTL_SECONDS``, a version below
      1, an empty content hash or a missing payment attempt (:class:`ValueError`).

    Note for the caller: :class:`GrantReplacementRefusedError` raised from a concurrent insert
    arrives after PostgreSQL has aborted the transaction. The surrounding transaction
    cannot continue and must be rolled back; there is no partial success to salvage.
    """
    if not isinstance(payment_attempt_id, uuid.UUID):
        # A NULL payment attempt would silently opt out of the partial unique index,
        # because PostgreSQL treats NULLs as distinct. "One live grant per attempt" would
        # then be an unenforced comment rather than a database guarantee.
        raise ValueError("a grant must name the payment attempt it authorizes")
    if not isinstance(amount, Money):
        raise ValueError(f"amount must be Money, got {type(amount).__name__}")
    if amount.minor <= 0:
        raise ValueError(f"a grant must authorize a positive amount, got {amount.minor}")
    if not isinstance(operation, Operation):
        raise ValueError(f"operation must be an Operation, got {operation!r}")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise ValueError(f"ttl_seconds must be int, got {type(ttl_seconds).__name__}")
    if not 1 <= ttl_seconds <= MAX_GRANT_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be within 1..{MAX_GRANT_TTL_SECONDS}, got {ttl_seconds}"
        )
    if checkout_ref.version < 1:
        raise ValueError(f"checkout version must be at least 1, got {checkout_ref.version}")
    if not checkout_ref.content_hash:
        raise ValueError("checkout_ref.content_hash is required; it is what binds the bytes")

    bound = require_tenant(session)
    if bound != tenant:
        # RLS would reject the INSERT anyway, but as an opaque row-security error. Naming
        # the mismatch here keeps a cross-tenant issue attempt legible in the audit trail.
        raise GrantTenantMismatchError(
            f"tenant {tenant} is not the tenant bound to this transaction ({bound})"
        )

    blocking = session.scalars(
        select(ExecutionGrant)
        .where(
            # Redundant under row-level security and kept anyway: this predicate is the
            # difference between a safe query and a cross-tenant read if the policy is
            # ever dropped, or if a future migration forgets FORCE ROW LEVEL SECURITY.
            ExecutionGrant.tenant_id == tenant,
            ExecutionGrant.payment_attempt_id == payment_attempt_id,
            or_(
                ExecutionGrant.status == _ISSUED_BLOCKS_ANY_OPERATION,
                and_(
                    ExecutionGrant.status == _CONSUMED_BLOCKS_SAME_OPERATION,
                    ExecutionGrant.operation == operation.value,
                ),
            ),
        )
        .limit(1)
    ).first()
    if blocking is not None:
        raise GrantReplacementRefusedError(
            f"payment attempt {payment_attempt_id} already holds a {blocking.status} grant "
            f"for {blocking.operation}; one live grant per attempt, and an uncertain "
            f"outcome is reconciled, never re-granted",
            grant_id=blocking.id,
        )

    grant_id = uuid7()
    statement = (
        insert(ExecutionGrant)
        .values(
            id=grant_id,
            tenant_id=tenant,
            checkout_id=checkout_ref.checkout_id,
            checkout_version=checkout_ref.version,
            content_hash=checkout_ref.content_hash,
            payment_attempt_id=payment_attempt_id,
            operation=operation.value,
            amount_minor=amount.minor,
            currency=amount.currency,
            kernel_decision_id=kernel_decision_id,
            status=GrantStatus.ISSUED,
            # Both timestamps come from the same transaction timestamp on the server.
            # An application-computed expires_at would let a pod with a fast clock mint a
            # grant that is already dead, or one with a slow clock mint a long-lived one.
            expires_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds),
        )
        .returning(ExecutionGrant)
    )
    try:
        grant: ExecutionGrant = session.scalars(statement).one()
    except IntegrityError as exc:
        if _is_one_active_per_attempt_violation(exc):
            raise GrantReplacementRefusedError(
                f"another transaction issued a grant for payment attempt "
                f"{payment_attempt_id} first; exactly one live grant per attempt"
            ) from exc
        raise
    return grant


def _is_one_active_per_attempt_violation(exc: IntegrityError) -> bool:
    """True only for the partial unique index that guards one live grant per attempt.

    Checked by SQLSTATE and constraint name rather than by matching the message text, so
    that a foreign-key or check-constraint failure is never dressed up as a grant
    conflict and retried by a caller that should have stopped.
    """
    orig: Any = exc.orig
    if getattr(orig, "sqlstate", None) != "23505":
        return False
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == _ONE_ACTIVE_PER_ATTEMPT


# -------------------------------------------------------------------------- consumption


def _locked_grant_query(
    grant_id: uuid.UUID, tenant_id: uuid.UUID
) -> Select[tuple[ExecutionGrant, bool]]:
    """Lock one grant row and ask the database whether it is past due.

    Three details carry the invariants:

    ``FOR UPDATE`` serializes consumers on the row itself. A second worker handed the same
    delivery blocks here until the first commits, then re-reads the committed row and sees
    ``CONSUMED``.

    ``now() >= expires_at`` is evaluated by PostgreSQL. ``now()`` is the transaction
    timestamp, so every expiry question asked inside one admission gets one consistent
    answer, and no application clock takes part in it.

    The tenant predicate duplicates row-level security on purpose. RLS is the enforcement
    point, but a grant id is a capability, and a lock taken on another tenant's row would
    be a cross-tenant write if the policy were ever dropped.
    """
    return (
        select(ExecutionGrant, (func.now() >= ExecutionGrant.expires_at).label("past_due"))
        .where(ExecutionGrant.id == grant_id, ExecutionGrant.tenant_id == tenant_id)
        .with_for_update(of=ExecutionGrant)
        # Without populate_existing a grant already in this session's identity map would
        # be returned with its stale attribute values, so the row would be locked and then
        # judged on what it looked like before the lock. The status check has to run
        # against the row as it is now.
        .execution_options(populate_existing=True)
    )


def consume_grant(
    session: Session, grant_id: uuid.UUID, expected_fields: GrantBinding
) -> ExecutionGrant:
    """Spend a grant's single use, or refuse and leave it untouched.

    Guarantees:

    * At most one caller ever succeeds for a given grant. The row is locked with
      ``SELECT ... FOR UPDATE`` and the transition is guarded ``ISSUED -> CONSUMED``.
    * Every bound field in ``expected_fields`` is compared against the admitted grant
      before the transition: tenant, checkout id, version, content hash, payment attempt,
      operation, amount in minor units and currency. Any difference refuses, and the grant
      stays ``ISSUED`` and usable by the correct command.
    * Expiry is decided by the database clock, so a grant past ``expires_at`` cannot be
      consumed even while its status still reads ``ISSUED`` because no sweeper has run.

    Refuses with :class:`GrantNotFoundError`, :class:`GrantTenantMismatchError`,
    :class:`GrantBindingMismatchError`, :class:`GrantAlreadyConsumedError`,
    :class:`GrantRevokedError` or :class:`GrantExpiredError`.

    After a successful consumption the caller owns the outcome. A provider timeout from
    here on is ``UNKNOWN`` and is reconciled under this same operation: there is no
    replacement grant and no blind retry, and :func:`issue_grant` will refuse to mint one.
    """
    bound = require_tenant(session)
    if expected_fields.tenant_id != bound:
        raise GrantTenantMismatchError(
            f"binding names tenant {expected_fields.tenant_id}, transaction is bound to {bound}",
            grant_id=grant_id,
        )

    row = session.execute(_locked_grant_query(grant_id, bound)).one_or_none()
    if row is None:
        raise GrantNotFoundError(
            f"no execution grant {grant_id} for this tenant", grant_id=grant_id
        )
    grant: ExecutionGrant = row[0]
    past_due = bool(row[1])

    deltas = _binding_deltas(grant, expected_fields)
    if deltas:
        # Checked before status: a command that is not the admitted command must be
        # refused as a substitution attempt, whatever state the grant happens to be in.
        fields = ", ".join(delta.field_path for delta in deltas)
        raise GrantBindingMismatchError(
            f"grant {grant_id} does not authorize this command; mismatched: {fields}",
            deltas=deltas,
            grant_id=grant_id,
        )

    # Order matters. A consumed grant is reported as consumed even when it is also past
    # due: reporting expiry first would tell the caller to re-admit an operation whose
    # provider request may already be in flight, which is how one charge becomes two.
    if grant.status == GrantStatus.CONSUMED:
        raise GrantAlreadyConsumedError(
            f"grant {grant_id} was already consumed at {grant.consumed_at}; "
            f"read the authoritative payment state, do not send the command again",
            grant_id=grant_id,
        )
    if grant.status == GrantStatus.REVOKED:
        raise GrantRevokedError(f"grant {grant_id} was revoked before use", grant_id=grant_id)
    if grant.status == GrantStatus.EXPIRED or past_due:
        raise GrantExpiredError(
            f"grant {grant_id} expired at {grant.expires_at} by the database clock",
            grant_id=grant_id,
        )
    if grant.status != GrantStatus.ISSUED:
        # Unreachable while the status check constraint holds; kept so that a future
        # state added to the schema fails closed here instead of falling through.
        raise GrantError(f"grant {grant_id} is {grant.status}, not ISSUED", grant_id=grant_id)

    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(ExecutionGrant)
            .where(
                ExecutionGrant.id == grant_id,
                # Redundant while the row lock above is held, and kept deliberately: if a
                # later refactor ever loses the lock, this turns a double consumption into
                # a zero-row update instead of a second charge.
                ExecutionGrant.status == GrantStatus.ISSUED,
            )
            .values(status=GrantStatus.CONSUMED, consumed_at=func.now())
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        raise GrantAlreadyConsumedError(
            f"grant {grant_id} was consumed by another transaction", grant_id=grant_id
        )
    session.refresh(grant)
    return grant


# ------------------------------------------------------------------ revocation + sweep


def revoke_grant(session: Session, grant_id: uuid.UUID) -> ExecutionGrant:
    """Withdraw one unused grant. Idempotent for a grant that is already ``REVOKED``.

    Refuses a ``CONSUMED`` grant with :class:`GrantAlreadyConsumedError`: the provider request
    may already have been sent, and rewriting the row to ``REVOKED`` would make the
    evidence claim an operation was withdrawn when it was in fact attempted. An expired
    grant is still revoked, so that Safe Mode leaves no ambiguity about intent.
    """
    bound = require_tenant(session)
    row = session.execute(_locked_grant_query(grant_id, bound)).one_or_none()
    if row is None:
        raise GrantNotFoundError(
            f"no execution grant {grant_id} for this tenant", grant_id=grant_id
        )
    grant: ExecutionGrant = row[0]

    if grant.status == GrantStatus.CONSUMED:
        raise GrantAlreadyConsumedError(
            f"grant {grant_id} was consumed at {grant.consumed_at} and cannot be revoked",
            grant_id=grant_id,
        )
    if grant.status == GrantStatus.REVOKED:
        return grant

    session.execute(
        update(ExecutionGrant)
        .where(ExecutionGrant.id == grant_id, ExecutionGrant.status != GrantStatus.CONSUMED)
        .values(status=GrantStatus.REVOKED)
        .execution_options(synchronize_session=False)
    )
    session.refresh(grant)
    return grant


def revoke_unused_grants(
    session: Session, *, operations: Collection[Operation] | None = None
) -> tuple[uuid.UUID, ...]:
    """Revoke every ``ISSUED`` grant of the bound tenant. Returns the ids revoked.

    This is Safe Mode's hand, specification 10.3.2: activation invalidates unused
    delegated grants. Pass ``operations`` to limit the sweep to the delegated payment
    paths; omit it to revoke everything still unused.

    ``CONSUMED`` grants are never touched. Safe Mode stops new money movement; it does not
    erase evidence of movement already attempted, and it does not convert an unknown
    outcome into a withdrawn one.

    The audit event for the revocation belongs to the caller: this function changes grant
    state and nothing else.
    """
    bound = require_tenant(session)
    statement = update(ExecutionGrant).where(
        # Explicit, not merely left to RLS. An unqualified mass UPDATE is the single most
        # dangerous statement in this module: if the policy is ever dropped or a migration
        # forgets FORCE ROW LEVEL SECURITY, this predicate is what stops one tenant's Safe
        # Mode from revoking every other tenant's live grants.
        ExecutionGrant.tenant_id == bound,
        ExecutionGrant.status == GrantStatus.ISSUED,
    )
    if operations is not None:
        if not operations:
            return ()
        statement = statement.where(
            ExecutionGrant.operation.in_([operation.value for operation in operations])
        )
    revoked: Sequence[uuid.UUID] = session.scalars(
        statement.values(status=GrantStatus.REVOKED)
        .returning(ExecutionGrant.id)
        .execution_options(synchronize_session=False)
    ).all()
    return tuple(revoked)


def expire_stale_grants(session: Session) -> tuple[uuid.UUID, ...]:
    """Mark past-due ``ISSUED`` grants ``EXPIRED``, by the database clock. Returns the ids.

    Never the enforcement point: :func:`consume_grant` re-evaluates ``expires_at`` against
    the server clock under the row lock, so a past-due grant this sweep has not reached is
    already unconsumable. Running it late is safe in that direction.

    It is not, however, optional. A past-due grant still reads ``ISSUED``, and an
    ``ISSUED`` row holds the one live slot on its payment attempt, so until this sweep runs
    that attempt can be re-admitted by nobody. Failing to sweep fails closed -- no money
    moves -- but it does strand the attempt, so this belongs on a schedule.
    """
    bound = require_tenant(session)
    expired: Sequence[uuid.UUID] = session.scalars(
        update(ExecutionGrant)
        .where(
            # Explicit alongside RLS, for the same reason as the revocation sweep.
            ExecutionGrant.tenant_id == bound,
            ExecutionGrant.status == GrantStatus.ISSUED,
            ExecutionGrant.expires_at <= func.now(),
        )
        .values(status=GrantStatus.EXPIRED)
        .returning(ExecutionGrant.id)
        .execution_options(synchronize_session=False)
    ).all()
    return tuple(expired)
