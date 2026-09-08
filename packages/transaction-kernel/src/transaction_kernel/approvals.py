"""Recorded buyer approvals, specification 10.2 and ADR 0003 D4a.

An approval is a buyer's decision about exact bytes: this checkout, this version, this
content hash, this amount in this currency, this action, under this Policy-at-Sale
Receipt. It is recorded by the trusted surface -- never by an agent -- and it is spent
exactly once, by admission, under the same version lock that admission already holds.

Why the approval is verified by row and not by trust
----------------------------------------------------
Admission receives an ``approval_id`` from the caller. If the kernel took the caller's
word that the id names a live approval of the right amount, an agent that obtained one
approval could replay it against a re-priced version, a different checkout or a larger
total. So :func:`consume_recorded` spends the approval with one guarded UPDATE whose
WHERE clause names every bound field and the database clock; either the row matched and
is now ``CONSUMED``, or nothing happened and the caller learns which check failed.

Lock order
----------
``approvals`` sits after ``checkout_versions`` and before ``reservations`` in the ADR D5
order. Every function here locks the version first, then touches the approval, then (if
at all) the reservation. The housekeeping sweep obeys the same order even though it
starts from the approvals table: it reads candidates without a lock, then locks each
version, then updates the approval, so it can never hold an approval row while waiting
for a version that admission holds while waiting for the approval.

What an expired approval does to its version
--------------------------------------------
A ``RECORDED`` approval lives only beside an ``APPROVED`` version: recording it is what
moves the version there, and every other exit from ``APPROVED`` (admission, rejection,
cancellation) changes the approval's status in the same transaction. The state table has
no edge back to ``APPROVAL_REQUIRED`` -- a version is never un-approved -- so when the
approval lapses the version cannot be re-offered for signature; its legal exits are
``EXECUTION_PENDING``, ``INVALIDATED``, ``CANCELLED`` and ``EXPIRED``. ``EXPIRED`` is the
honest one: nobody cancelled, nothing was invalidated, the window closed. A corrected
purchase is a new version with a new hash and a new approval, exactly as for any other
retired version. The reservation is released with cause ``CANCELLED`` rather than
``EXPIRED`` because, to the reservation module, ``EXPIRED`` means the *hold* lapsed on the
database clock, which it has not; the platform is withdrawing a live hold for a version
it has retired, and that is a confirmed cancellation.

Saying no now is not the same as saying no
------------------------------------------
:func:`reject_approval` is what a buyer means by "cancel this": the version is retired and
the stock goes back on the shelf. It was for a long time the only "no" the kernel had, so
a buyer who wanted a minute to think had to be recorded as having cancelled their
purchase, and came back to a dead version and an empty hold. :func:`defer_approval` is the
other answer. It writes ``approval.held`` and changes nothing at all: no transition, no
approval row, no touch on the reservation or the cart. The version is still
``APPROVAL_REQUIRED``, the same hash is still approvable, and the only thing that has
happened is that the platform now has evidence the buyer was asked and did not say yes --
which is exactly what a surface needs to stop asking again, and exactly what an auditor
needs to see that nobody was charged for a question they declined to answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from commerce_domain import DomainError, Money, uuid7
from sqlalchemy import Row, TextClause, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, reservations
from .checkouts import (
    AGGREGATE_TYPE,
    CheckoutStateError,
    apply_transition,
    lock_version,
    require_context,
    sync_head,
)
from .contracts import ActorType, AgentPrincipal, CheckoutRef
from .recovery import RecoveryCode
from .states import NON_TERMINAL_PAYMENT_STATES, CheckoutState

__all__ = [
    "DEFAULT_ACTION",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "MAX_APPROVAL_TTL_SECONDS",
    "ApprovalConflictError",
    "ApprovalError",
    "ApprovalHold",
    "ApprovalInvalidReason",
    "ApprovalNotValidError",
    "ApprovalRecord",
    "ApprovalRejection",
    "ApprovalStateError",
    "ApprovalStatus",
    "ApprovalTenantError",
    "consume_recorded",
    "defer_approval",
    "expire_stale_approvals",
    "record_approval",
    "reject_approval",
]

#: ADR D13. Long enough to hand off to the payment surface; short enough that a decision
#: made this morning cannot be spent this evening against stock priced differently.
DEFAULT_APPROVAL_TTL_SECONDS: Final = 600

#: A day. Nothing legitimate keeps consent open longer without asking again.
MAX_APPROVAL_TTL_SECONDS: Final = 86_400

#: The operation a human-present checkout approves. Matches ``Operation`` and the outbox
#: command type byte-for-byte so the grant, the command and the approval all name one thing.
DEFAULT_ACTION: Final = "PAYMENT_CREATE_ORDER"

#: The partial unique index that admits one RECORDED approval per version (migration
#: c6ffa021cbb0). Matched by name so no other integrity failure is reported as a race.
_ONE_RECORDED_PER_VERSION: Final = "uq_approvals_one_recorded_per_version"
_UNIQUE_VIOLATION: Final = "23505"


class ApprovalStatus(StrEnum):
    """Mirrors the ``status_enum`` check on ``approvals``."""

    RECORDED = "RECORDED"
    CONSUMED = "CONSUMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class ApprovalInvalidReason(StrEnum):
    """Why :func:`consume_recorded` refused. Closed set; the code is always the same."""

    MISSING = "missing"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    INVALIDATED = "invalidated"
    MISMATCH = "mismatch"


# --------------------------------------------------------------------------- errors


class ApprovalError(DomainError):
    """An approval operation was refused. ``code`` is what the caller may do next."""

    code: RecoveryCode = RecoveryCode.POLICY_EXCEPTION

    def __init__(self, reason: str, message: str, *, code: RecoveryCode | None = None) -> None:
        self.reason = reason
        if code is not None:
            self.code = code
        super().__init__(f"{reason}: {message}")


class ApprovalTenantError(ApprovalError):
    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class ApprovalStateError(ApprovalError):
    """The locked version is not one this approval operation can act on."""

    code = RecoveryCode.STALE_CHECKOUT


class ApprovalConflictError(ApprovalError):
    """A RECORDED approval for this version already exists (lost the race)."""

    code = RecoveryCode.CONCURRENT_OPERATION


class ApprovalNotValidError(ApprovalError):
    """The approval named at admission cannot be spent. Always ``AUTHORITY_INSUFFICIENT``:
    whatever the detail, the caller does not hold a live decision for these bytes."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT

    def __init__(self, why: ApprovalInvalidReason, approval_id: uuid.UUID) -> None:
        self.why = why
        self.approval_id = approval_id
        super().__init__(why.value, f"approval {approval_id} cannot be consumed: {why.value}")


# --------------------------------------------------------------------------- values


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One approvals row as stored."""

    approval_id: uuid.UUID
    tenant_id: uuid.UUID
    checkout: CheckoutRef
    amount: Money
    action: str
    status: ApprovalStatus
    policy_receipt_hash: str | None
    issued_at: datetime
    expires_at: datetime
    authority_id: uuid.UUID | None = None
    authority_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRejection:
    checkout: CheckoutRef
    from_state: CheckoutState
    approval_ids: tuple[uuid.UUID, ...]
    reservation_release: RecoveryCode


@dataclass(frozen=True, slots=True)
class ApprovalHold:
    """One recorded "not now". Everything on it describes state that did not change.

    ``state`` is the version's status read under the lock rather than the constant
    ``APPROVAL_REQUIRED``, so a caller echoing it to a buyer is echoing the row and not a
    literal this module hoped was true. ``reservation`` is the hold as the database clock
    saw it at that moment, which is how a surface can honestly say how much longer the
    stock is being kept. ``held_at`` and ``event_id`` come from the audit row, because the
    event *is* the whole of what this operation produced.
    """

    checkout: CheckoutRef
    state: CheckoutState
    reservation: reservations.ReservationView | None
    held_at: datetime
    event_id: uuid.UUID


# ------------------------------------------------------------------------------- SQL

_APPROVAL_COLUMNS: Final = (
    "id, tenant_id, checkout_id, checkout_version, content_hash, policy_receipt_hash, "
    "amount_minor, currency, action, authority_id, authority_epoch, status, issued_at, "
    "expires_at"
)


def _stmt(*parts: str) -> TextClause:
    """Compose one statement from the SQL fragments in this module.

    Every argument is a literal written here or a module-level constant; nothing from a
    request, a model or a row is ever joined into SQL text, and every variable value
    travels as a bound parameter.
    """
    return text(" ".join(parts))


_INSERT = _stmt(
    "INSERT INTO approvals (id, tenant_id, checkout_id, checkout_version, content_hash,",
    "policy_receipt_hash, amount_minor, currency, action, authority_id, authority_epoch,",
    "status, expires_at)",
    "VALUES (:id, :t, :c, :v, :h, :rh, :amt, :cur, :action, :auth, :epoch, :status,",
    "now() + (CAST(:ttl AS integer) * INTERVAL '1 second'))",
    "RETURNING",
    _APPROVAL_COLUMNS,
)

# The whole contract in one WHERE clause. Every bound field and the database clock have to
# agree, or no row changes. There is no read-then-write here to race against.
_CONSUME = _stmt(
    "UPDATE approvals SET status = :consumed",
    "WHERE tenant_id = :t AND id = :id AND status = :recorded AND expires_at > now()",
    "AND checkout_id = :c AND checkout_version = :v AND content_hash = :h",
    "AND amount_minor = :amt AND currency = :cur AND action = :action",
    "RETURNING",
    _APPROVAL_COLUMNS,
)

_DIAGNOSE = text(
    "SELECT status, (expires_at <= now()) AS lapsed FROM approvals "
    "WHERE tenant_id = :t AND id = :id"
)

_INVALIDATE_RECORDED = text(
    "UPDATE approvals SET status = :invalidated "
    "WHERE tenant_id = :t AND checkout_id = :c AND checkout_version = :v "
    "AND status = :recorded RETURNING id"
)

_STALE_CANDIDATES = text(
    "SELECT id, checkout_id, checkout_version, content_hash FROM approvals "
    "WHERE tenant_id = :t AND status = :recorded AND expires_at <= now() "
    "ORDER BY expires_at LIMIT :limit"
)

# Re-checked against the clock under the version lock: a candidate read a moment ago may
# have been consumed by an admission that committed in between.
_EXPIRE_ONE = text(
    "UPDATE approvals SET status = :expired "
    "WHERE tenant_id = :t AND id = :id AND status = :recorded AND expires_at <= now() "
    "RETURNING id"
)

_RECEIPT_BINDING = text(
    "SELECT receipt_hash, content ->> 'checkout_hash' AS bound_hash "
    "FROM policy_at_sale_receipts WHERE tenant_id = :t AND id = :rid "
    "AND checkout_id = :c AND checkout_version = :v"
)

_FIND_LIVE_ATTEMPT = text(
    "SELECT id FROM payment_attempts WHERE tenant_id = :t AND checkout_id = :c "
    "AND checkout_version = :v AND status = ANY(:states) LIMIT 1"
)


# -------------------------------------------------------------------------- internals


def _record(row: Row[Any]) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.id,
        tenant_id=row.tenant_id,
        checkout=CheckoutRef(row.checkout_id, int(row.checkout_version), str(row.content_hash)),
        amount=Money(int(row.amount_minor), str(row.currency)),
        action=str(row.action),
        status=ApprovalStatus(row.status),
        policy_receipt_hash=None
        if row.policy_receipt_hash is None
        else str(row.policy_receipt_hash),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        authority_id=row.authority_id,
        authority_epoch=None if row.authority_epoch is None else int(row.authority_epoch),
    )


def _principal_for(principal: AgentPrincipal, tenant_id: uuid.UUID) -> AgentPrincipal:
    if not isinstance(principal, AgentPrincipal):
        raise ApprovalError("bad_principal", "principal must be an AgentPrincipal")
    if principal.tenant_id != tenant_id:
        raise ApprovalTenantError(
            "principal_tenant_mismatch",
            f"principal belongs to tenant {principal.tenant_id}, not {tenant_id}",
        )
    return principal


def _amount_payload(amount: Money) -> dict[str, Any]:
    return {"currency": amount.currency, "minor": amount.minor}


def _locked_version(session: Session, tenant_id: uuid.UUID, checkout: CheckoutRef) -> Any:
    """Lock the version, translating the checkout module's refusal into ours."""
    try:
        return lock_version(session, tenant_id=tenant_id, checkout=checkout)
    except CheckoutStateError as exc:
        raise ApprovalStateError(exc.reason, str(exc), code=exc.code) from exc


def _is_one_recorded_violation(exc: IntegrityError) -> bool:
    orig: Any = exc.orig
    if getattr(orig, "sqlstate", None) != _UNIQUE_VIOLATION:
        return False
    return (
        getattr(getattr(orig, "diag", None), "constraint_name", None) == _ONE_RECORDED_PER_VERSION
    )


# ----------------------------------------------------------------------------- record


def record_approval(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    amount: Money,
    principal: AgentPrincipal,
    correlation_id: uuid.UUID,
    action: str = DEFAULT_ACTION,
    expires_in_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    authority_id: uuid.UUID | None = None,
    authority_epoch: int | None = None,
) -> ApprovalRecord:
    """Record a buyer's decision and move the version ``APPROVAL_REQUIRED -> APPROVED``.

    Guarantees, all inside the caller's transaction:

    * the version row is locked first, so a concurrent supersede or rejection serializes
      behind this call and sees its result;
    * the approval names exactly what the buyer saw -- the stored ``content_hash``, the
      stored ``total_minor``/``currency`` and the bound ``policy_receipt_hash`` -- and
      every one of those is compared against the caller's copy before the row is written,
      so an approval can never be recorded for an amount the version does not carry;
    * ``expires_at`` is ``now() + expires_in_seconds`` on the database clock;
    * one ``RECORDED`` approval per version, enforced by the partial unique index and
      reported as :class:`ApprovalConflictError` rather than as a driver error;
    * the head mirrors ``APPROVED`` and ``approval.recorded`` is audited with the hash
      and the amount in its payload.

    Refuses with :class:`ApprovalStateError`: a version that is missing, invalidated, not
    at ``APPROVAL_REQUIRED`` (``DUPLICATE_OPERATION`` when already ``APPROVED``), whose
    hash or total differ from the caller's (``STALE_CHECKOUT``), or whose receipt binding
    is absent or does not verify (``HUMAN_REVIEW_REQUIRED``).
    """
    require_context(session, tenant_id)
    _principal_for(principal, tenant_id)
    if not isinstance(correlation_id, uuid.UUID):
        raise ApprovalError("bad_correlation_id", "correlation_id must be a UUID")
    if not isinstance(amount, Money) or amount.minor < 0:
        raise ApprovalError("bad_amount", "amount must be non-negative Money")
    if not isinstance(action, str) or not action or len(action) > 32:
        raise ApprovalError("bad_action", "action must be a non-empty string of <= 32 chars")
    if isinstance(expires_in_seconds, bool) or not isinstance(expires_in_seconds, int):
        raise ApprovalError("bad_ttl", "expires_in_seconds must be an int")
    if not 1 <= expires_in_seconds <= MAX_APPROVAL_TTL_SECONDS:
        raise ApprovalError(
            "bad_ttl", f"expires_in_seconds must be within 1..{MAX_APPROVAL_TTL_SECONDS}"
        )

    locked = _locked_version(session, tenant_id, checkout)
    if locked.invalidated_at is not None:
        raise ApprovalStateError("version_invalidated", "an invalidated version is never approved")
    if locked.status is CheckoutState.APPROVED:
        raise ApprovalStateError(
            "already_approved",
            "this version already carries a recorded approval",
            code=RecoveryCode.DUPLICATE_OPERATION,
        )
    if locked.status is not CheckoutState.APPROVAL_REQUIRED:
        raise ApprovalStateError(
            "wrong_status", f"approval is recorded at APPROVAL_REQUIRED, not {locked.status.value}"
        )
    if locked.policy_receipt_id is None or locked.policy_receipt_hash is None:
        raise ApprovalStateError(
            "receipt_not_bound",
            "no Policy-at-Sale Receipt governs this version; an approval without terms "
            "is not a decision about anything",
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
        )
    binding = session.execute(
        _RECEIPT_BINDING,
        {
            "t": tenant_id,
            "rid": locked.policy_receipt_id,
            "c": checkout.checkout_id,
            "v": checkout.version,
        },
    ).one_or_none()
    if binding is None:
        raise ApprovalStateError(
            "receipt_missing",
            f"receipt {locked.policy_receipt_id} does not exist for this version",
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
        )
    if (
        binding.receipt_hash != locked.policy_receipt_hash
        or binding.bound_hash != locked.content_hash
    ):
        raise ApprovalStateError(
            "receipt_binding_broken",
            "the receipt bound to this version does not describe it",
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
        )
    if locked.total != amount:
        raise ApprovalStateError(
            "amount_mismatch",
            f"the buyer approved {amount} but version {checkout.version} totals {locked.total}",
        )

    approval_id = uuid7()
    try:
        with session.begin_nested():
            row = session.execute(
                _INSERT,
                {
                    "id": approval_id,
                    "t": tenant_id,
                    "c": checkout.checkout_id,
                    "v": checkout.version,
                    "h": checkout.content_hash,
                    "rh": locked.policy_receipt_hash,
                    "amt": amount.minor,
                    "cur": amount.currency,
                    "action": action,
                    "auth": authority_id,
                    "epoch": authority_epoch,
                    "status": ApprovalStatus.RECORDED.value,
                    "ttl": expires_in_seconds,
                },
            ).one()
            record = _record(row)
    except IntegrityError as exc:
        if _is_one_recorded_violation(exc):
            raise ApprovalConflictError(
                "approval_already_recorded",
                f"a RECORDED approval already exists for version {checkout.version}",
            ) from exc
        raise

    apply_transition(
        session,
        tenant_id=tenant_id,
        checkout=checkout,
        current=CheckoutState.APPROVAL_REQUIRED,
        target=CheckoutState.APPROVED,
    )
    head_updated = sync_head(
        session,
        tenant_id=tenant_id,
        checkout_id=checkout.checkout_id,
        version=checkout.version,
        status=CheckoutState.APPROVED,
    )
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout.checkout_id,
        event_type="approval.recorded",
        actor_type=principal.actor_type,
        principal_id=principal.principal_id,
        payload={
            "approval_id": approval_id,
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "amount": _amount_payload(amount),
            "action": action,
            "policy_receipt_hash": locked.policy_receipt_hash,
            "expires_in_seconds": expires_in_seconds,
            "authority_id": authority_id,
            "authority_epoch": authority_epoch,
            "head_updated": head_updated,
        },
        correlation_id=correlation_id,
    )
    return record


# ---------------------------------------------------------------------------- consume


def consume_recorded(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    approval_id: uuid.UUID,
    checkout: CheckoutRef,
    amount: Money,
    action: str = DEFAULT_ACTION,
    correlation_id: uuid.UUID | None = None,
    actor: ActorType = ActorType.SYSTEM,
    principal_id: str | None = None,
) -> ApprovalRecord:
    """Spend one approval, once, for exactly the bytes, amount and action it recorded.

    Called by admission after it holds the version lock (ADR D4a). One guarded UPDATE
    moves ``RECORDED -> CONSUMED`` only where the tenant, id, checkout, version, content
    hash, amount, currency, ``action`` and ``expires_at > now()`` all match.

    ``action`` is in that list because the column has always been written and never read.
    An approval records *what* the buyer agreed to do -- create a payment, execute a
    refund, debit a delegated authority -- and without the comparison a consent given for
    one of those would spend against another for the same bytes and the same amount.

    A second call finds the
    row ``CONSUMED``; a replay against other bytes finds no match; a lapsed approval finds
    the clock against it. None of those change anything.

    Raises :class:`ApprovalNotValidError` with ``why`` in
    :class:`ApprovalInvalidReason` and code ``AUTHORITY_INSUFFICIENT``. The diagnosis
    is a plain read after the failed UPDATE, for the caller's audit payload; it never
    widens the match.

    Audits ``approval.consumed``. ``correlation_id`` should be the admission's; a fresh
    one is minted only when none is supplied, so the evidence is never left without one.
    """
    require_context(session, tenant_id)
    if not isinstance(approval_id, uuid.UUID):
        raise ApprovalError("bad_approval_id", "approval_id must be a UUID")
    if not isinstance(amount, Money):
        raise ApprovalError("bad_amount", "amount must be Money")

    row = session.execute(
        _CONSUME,
        {
            "consumed": ApprovalStatus.CONSUMED.value,
            "recorded": ApprovalStatus.RECORDED.value,
            "t": tenant_id,
            "id": approval_id,
            "c": checkout.checkout_id,
            "v": checkout.version,
            "h": checkout.content_hash,
            "amt": amount.minor,
            "cur": amount.currency,
            "action": action,
        },
    ).one_or_none()
    if row is None:
        found = session.execute(_DIAGNOSE, {"t": tenant_id, "id": approval_id}).one_or_none()
        if found is None:
            why = ApprovalInvalidReason.MISSING
        elif found.status == ApprovalStatus.CONSUMED.value:
            why = ApprovalInvalidReason.CONSUMED
        elif found.status == ApprovalStatus.INVALIDATED.value:
            why = ApprovalInvalidReason.INVALIDATED
        elif found.status == ApprovalStatus.EXPIRED.value or bool(found.lapsed):
            why = ApprovalInvalidReason.EXPIRED
        else:
            why = ApprovalInvalidReason.MISMATCH
        raise ApprovalNotValidError(why, approval_id)

    record = _record(row)
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout.checkout_id,
        event_type="approval.consumed",
        actor_type=actor,
        principal_id=principal_id,
        payload={
            "approval_id": approval_id,
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "amount": _amount_payload(amount),
            "action": record.action,
        },
        correlation_id=correlation_id if correlation_id is not None else uuid7(),
    )
    return record


# ----------------------------------------------------------------------------- reject


def reject_approval(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    principal: AgentPrincipal,
    reason: str,
    correlation_id: uuid.UUID,
) -> ApprovalRejection:
    """The buyer declines. The version is retired and its stock goes back on the shelf.

    From ``APPROVAL_REQUIRED`` (declined before deciding) or ``APPROVED`` (changed their
    mind before submitting) the version moves to ``CANCELLED``; any ``RECORDED`` approval
    becomes ``INVALIDATED``; the reservation is released with cause ``CANCELLED``; the
    head mirrors ``CANCELLED``; ``approval.rejected`` is audited with the reason.

    Refuses (:class:`ApprovalStateError`) a version in any other state, an already
    cancelled version (``DUPLICATE_OPERATION``), a hash the buyer did not see, and a
    version with a live payment attempt (``CONCURRENT_OPERATION``): once admission has
    run, cancellation goes through :func:`transaction_kernel.checkouts.cancel`, which
    knows how to retire the attempt and revoke the grant.
    """
    require_context(session, tenant_id)
    _principal_for(principal, tenant_id)
    if not isinstance(correlation_id, uuid.UUID):
        raise ApprovalError("bad_correlation_id", "correlation_id must be a UUID")
    if not isinstance(reason, str) or not reason:
        raise ApprovalError("bad_reason", "reason must be a non-empty stable key")

    live = session.execute(
        _FIND_LIVE_ATTEMPT,
        {
            "t": tenant_id,
            "c": checkout.checkout_id,
            "v": checkout.version,
            "states": [state.value for state in sorted(NON_TERMINAL_PAYMENT_STATES)],
        },
    ).one_or_none()

    locked = _locked_version(session, tenant_id, checkout)
    if locked.status is CheckoutState.CANCELLED:
        raise ApprovalStateError(
            "already_cancelled",
            "this version is already cancelled",
            code=RecoveryCode.DUPLICATE_OPERATION,
        )
    if locked.status not in (CheckoutState.APPROVAL_REQUIRED, CheckoutState.APPROVED):
        raise ApprovalStateError(
            "wrong_status",
            f"an approval is rejected from APPROVAL_REQUIRED or APPROVED, not "
            f"{locked.status.value}",
        )
    if live is not None:
        raise ApprovalStateError(
            "attempt_in_flight",
            f"payment attempt {live.id} exists for this version; use checkouts.cancel",
            code=RecoveryCode.CONCURRENT_OPERATION,
        )

    invalidated = tuple(
        session.execute(
            _INVALIDATE_RECORDED,
            {
                "invalidated": ApprovalStatus.INVALIDATED.value,
                "recorded": ApprovalStatus.RECORDED.value,
                "t": tenant_id,
                "c": checkout.checkout_id,
                "v": checkout.version,
            },
        )
        .scalars()
        .all()
    )
    apply_transition(
        session,
        tenant_id=tenant_id,
        checkout=checkout,
        current=locked.status,
        target=CheckoutState.CANCELLED,
    )
    release = reservations.release(
        session,
        checkout_id=checkout.checkout_id,
        checkout_version=checkout.version,
        cause=reservations.ReleaseCause.CANCELLED,
    )
    head_updated = sync_head(
        session,
        tenant_id=tenant_id,
        checkout_id=checkout.checkout_id,
        version=checkout.version,
        status=CheckoutState.CANCELLED,
    )
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout.checkout_id,
        event_type="approval.rejected",
        actor_type=principal.actor_type,
        principal_id=principal.principal_id,
        payload={
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "from": locked.status.value,
            "reason": reason,
            "approval_ids": list(invalidated),
            "reservation_release": release.code.value,
            "head_updated": head_updated,
        },
        correlation_id=correlation_id,
    )
    return ApprovalRejection(
        checkout=checkout,
        from_state=locked.status,
        approval_ids=invalidated,
        reservation_release=release.code,
    )


# --------------------------------------------------------------------------------- hold


def defer_approval(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    principal: AgentPrincipal,
    reason: str,
    correlation_id: uuid.UUID,
) -> ApprovalHold:
    """The buyer was asked and said not now. Write that down; change nothing else.

    This is the "no" that is not a cancellation. It applies no transition, records no
    approval, releases no hold and does not touch the cart: after it returns, the
    version is exactly as approvable as it was before, against the same content hash, and
    the buyer can come back and say yes. All it produces is one ``approval.held`` audit
    event, which is the point -- a surface that has to remember whether it already asked
    is remembering it in the browser, where nothing can prove it afterwards.

    The hash is echoed and verified, exactly as it is on :func:`record_approval` and
    :func:`reject_approval`. A decline that does not name the bytes being declined is
    evidence about nothing, and a client showing a stale card would otherwise write a
    "the buyer saw this and passed" event about a card the buyer never saw.

    The version is still locked first, even though nothing is written to it. Two reasons:
    the status this reads has to be the status at the moment of the decision rather than
    one an approval committing alongside could have already moved, and the lock order in
    the module docstring is the only thing keeping this out of a deadlock with the
    admission that may be running for the same version.

    Refuses with :class:`ApprovalStateError` from every state except ``APPROVAL_REQUIRED``,
    with code ``STALE_CHECKOUT``. There is nothing to decline at ``APPROVED`` -- a decision
    is already recorded there and withdrawing it is :func:`reject_approval`'s job -- and
    from ``EXECUTION_PENDING`` onwards money may already be moving, where the honest answer
    is a cancellation that knows how to retire an attempt, not a note in the log.
    """
    require_context(session, tenant_id)
    _principal_for(principal, tenant_id)
    if not isinstance(correlation_id, uuid.UUID):
        raise ApprovalError("bad_correlation_id", "correlation_id must be a UUID")
    if not isinstance(reason, str) or not reason:
        raise ApprovalError("bad_reason", "reason must be a non-empty stable key")

    locked = _locked_version(session, tenant_id, checkout)
    if locked.invalidated_at is not None:
        raise ApprovalStateError(
            "version_invalidated", "an invalidated version is not waiting for an answer"
        )
    if locked.status is not CheckoutState.APPROVAL_REQUIRED:
        raise ApprovalStateError(
            "wrong_status",
            f"a hold is recorded at APPROVAL_REQUIRED, not {locked.status.value}",
        )

    # Read without a lock, and deliberately: nothing here writes to the reservation, so
    # holding its row for the rest of the caller's transaction would block an admission
    # that has every right to spend the hold while the buyer is still thinking.
    outcome = reservations.check_validity(
        session,
        checkout_id=checkout.checkout_id,
        checkout_version=checkout.version,
        lock=False,
    )
    view = outcome.reservation
    event = audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout.checkout_id,
        event_type="approval.held",
        actor_type=principal.actor_type,
        principal_id=principal.principal_id,
        payload={
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "version_status": locked.status.value,
            "reason": reason,
            "reservation_id": None if view is None else view.reservation_id,
            # Seconds and a status rather than a timestamp: an audit payload has to
            # survive a JSONB round trip, and the number a buyer was shown on the
            # countdown is the fact worth keeping anyway.
            "reservation_status": None if view is None else view.status.value,
            "reservation_seconds_remaining": None if view is None else view.seconds_remaining,
        },
        correlation_id=correlation_id,
    )
    return ApprovalHold(
        checkout=checkout,
        state=locked.status,
        reservation=view,
        held_at=event.occurred_at,
        event_id=event.event_id,
    )


# ------------------------------------------------------------------------ housekeeping


def expire_stale_approvals(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    limit: int = 100,
    correlation_id: uuid.UUID | None = None,
) -> int:
    """Retire approvals whose window closed, by the database clock. Returns the count.

    Hygiene, not authority: :func:`consume_recorded` already refuses a lapsed approval
    whether or not this has run. What the sweep adds is an honest ``status`` column and a
    version that is not stranded in ``APPROVED`` with no approval to spend -- see the
    module docstring for why that version becomes ``EXPIRED`` and why its hold is released
    with cause ``CANCELLED``.

    Lock order is kept even though the work starts from the approvals table: candidates
    are read without a lock, then for each one the version is locked, then the approval is
    updated under a re-check of the clock. A candidate that an admission consumed in the
    meantime is skipped, and a version whose status is no longer ``APPROVED`` is left
    alone with the fact recorded in the audit payload, because a ``RECORDED`` approval
    beside any other status is a writer bug this sweep must not paper over.
    """
    require_context(session, tenant_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ApprovalError("bad_limit", "limit must be a positive int batch size")
    sweep_correlation = correlation_id if correlation_id is not None else uuid7()

    candidates = session.execute(
        _STALE_CANDIDATES,
        {"t": tenant_id, "recorded": ApprovalStatus.RECORDED.value, "limit": limit},
    ).all()

    expired = 0
    for candidate in candidates:
        checkout = CheckoutRef(
            candidate.checkout_id, int(candidate.checkout_version), str(candidate.content_hash)
        )
        try:
            locked = lock_version(
                session, tenant_id=tenant_id, checkout=checkout, verify_hash=False
            )
        except CheckoutStateError:
            # The version is gone for this tenant; the approval row is retired on its own.
            locked = None

        done = session.execute(
            _EXPIRE_ONE,
            {
                "expired": ApprovalStatus.EXPIRED.value,
                "recorded": ApprovalStatus.RECORDED.value,
                "t": tenant_id,
                "id": candidate.id,
            },
        ).one_or_none()
        if done is None:
            continue
        expired += 1

        version_transition: dict[str, Any] | None = None
        release_code: str | None = None
        if locked is not None and locked.status is CheckoutState.APPROVED:
            apply_transition(
                session,
                tenant_id=tenant_id,
                checkout=checkout,
                current=CheckoutState.APPROVED,
                target=CheckoutState.EXPIRED,
            )
            release_code = reservations.release(
                session,
                checkout_id=checkout.checkout_id,
                checkout_version=checkout.version,
                cause=reservations.ReleaseCause.CANCELLED,
            ).code.value
            sync_head(
                session,
                tenant_id=tenant_id,
                checkout_id=checkout.checkout_id,
                version=checkout.version,
                status=CheckoutState.EXPIRED,
            )
            version_transition = {
                "from": CheckoutState.APPROVED.value,
                "to": CheckoutState.EXPIRED.value,
            }

        audit.append(
            session,
            tenant=tenant_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=checkout.checkout_id,
            event_type="approval.expired",
            actor_type=ActorType.SYSTEM,
            principal_id=None,
            payload={
                "approval_id": candidate.id,
                "version": checkout.version,
                "content_hash": checkout.content_hash,
                "version_status": None if locked is None else locked.status.value,
                "version_transition": version_transition,
                "reservation_release": release_code,
            },
            correlation_id=sweep_correlation,
        )
    return expired
