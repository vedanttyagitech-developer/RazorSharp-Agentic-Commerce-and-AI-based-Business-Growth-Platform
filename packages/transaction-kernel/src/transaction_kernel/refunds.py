"""Refunds: admission, results, reconciliation and escalation. Specification 10.5, 10.6,
10.8, 11.4 and ADR 0003 D10.

Why refunds are a kernel path
-----------------------------
A refund moves money, so it is admitted the way a payment is: one transaction, locks in
the ADR 0003 D5 order, every figure re-read from locked rows, exactly one ``refunds`` row
and exactly one Execution Grant on success, a structured denial otherwise, and the audit
row committed with the state change. The worker that talks to Razorpay holds the grant
and the refund's stable idempotency key; it never decides an amount.

The ledger rule
---------------
``captured - already refunded`` is the only number a refund may be measured against, and
"already refunded" counts every refund that *may exist at the provider*: ``PENDING`` is
reserved money, ``UNKNOWN``, ``RECONCILING`` and ``ESCALATED`` may already have landed,
``PROCESSED`` has. Only ``FAILED`` -- a provider-confirmed refusal -- releases its amount.
Counting ``PENDING`` as reserved is what stops two partial refunds admitted back to back
from together exceeding the capture; counting ``UNKNOWN`` is what stops a lost response
from becoming a second refund of the same money.

Two failure states, and they are not interchangeable
----------------------------------------------------
``REFUND_UNKNOWN`` means the provider's answer was lost and a refund may already exist:
its only exit is :func:`reconcile_refund`, and no function here issues a grant from it. A
verified absence lets the caller admit a *new* refund with a *new* grant; nothing re-arms
the old one. ``REFUND_FAILED`` means the provider confirmed no refund exists, so
:func:`admit_refund` may run again and mints a fresh row and a fresh grant. Conflating
the two is how a buyer is paid twice, so the distinction is enforced by the state table in
:mod:`transaction_kernel.states`, not by this module's opinion.

Per-refund grant binding (ADR 0003 D10)
---------------------------------------
Each refund's grant carries the refund's id. The "no replacement after consumption" rule
therefore keys on ``(REFUND_EXECUTE, refund_id)``: a second partial refund is a second
row with its own grant, admitted only after the first grant is consumed and the attempt
has settled into ``PARTIALLY_REFUNDED``. While a refund is ``REFUND_PENDING`` no second
one is admitted, which is also what the one-live-grant-per-attempt index enforces.

Human review
------------
:func:`escalate_refund` freezes the attempt in ``ESCALATED`` and opens exactly one case
per ``case_key`` (specification 6.4.3: tenant + order + refund + reason family). No
``support_cases`` table exists yet, so the case is the ``human_review.opened`` audit event
and the exactly-once guarantee comes from the attempt row lock plus a scan of the attempt's
own stream for the same key before appending. When the table lands, the same key is what
``open_or_get_support_case`` must be called with.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal, cast

from commerce_domain import (
    ActorType,
    AdmissionDecision,
    AgentPrincipal,
    CheckoutRef,
    Delta,
    Money,
    RecoveryCode,
    canonical_hash,
    uuid7,
)
from platform_db import require_tenant
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import audit, grants, safe_mode
from .contracts import Operation
from .metrics import increment
from .receipts import RefundWindow, database_now_ms, refund_window_at_sale
from .states import (
    PAYMENT_TRANSITIONS,
    UNCERTAIN_PAYMENT_STATES,
    InvalidTransitionError,
    PaymentState,
    assert_transition,
    can_transition,
)

__all__ = [
    "AGGREGATE_TYPE",
    "MAX_RECONCILIATION_ATTEMPTS",
    "PROVIDER_ORIGINATED_REASON",
    "REFUND_GRANT_TTL_SECONDS",
    "RESERVING_STATUSES",
    "STALE_CAPTURE_REASON",
    "RefundAdmission",
    "RefundError",
    "RefundEscalation",
    "RefundLedger",
    "RefundLedgerError",
    "RefundNotFoundError",
    "RefundReconciliation",
    "RefundStateError",
    "RefundStatus",
    "RefundTenantMismatchError",
    "RefundTransition",
    "RefundUsageError",
    "admit_refund",
    "admit_stale_capture_refund",
    "escalate_refund",
    "human_review_case_key",
    "refundable_now",
    "reconcile_refund",
    "record_provider_originated_refund",
    "record_refund_result",
    "refund_idempotency_key",
]

#: Audit stream every refund event is appended to. Refund states live on the payment
#: attempt (specification 10.5), so its stream is where a reviewer reads them.
AGGREGATE_TYPE: Final = "payment_attempt"

#: ADR 0003 D13. A refund grant is handed straight to the outbox; it has no reason to
#: outlive one lease plus one provider round trip.
REFUND_GRANT_TTL_SECONDS: Final = 300

#: ADR 0003 D13. The caller escalates once this many reconciliation rounds have not
#: resolved a ``REFUND_UNKNOWN``; recorded so the audit trail can say the bound was met.
MAX_RECONCILIATION_ATTEMPTS: Final = 6

#: ``refunds.reason_code`` of the automatic full refund of a stale capture (10.8), and of
#: a refund first seen from the provider (11.3). Both are stable keys, never sentences.
STALE_CAPTURE_REASON: Final = "STALE_CAPTURE"
PROVIDER_ORIGINATED_REASON: Final = "PROVIDER_ORIGINATED"

#: Column widths from ``platform_db.schema.Refund``, checked here so an over-long value is
#: refused by name instead of aborting the caller's transaction as a driver error.
_MAX_REASON_CODE: Final = 64
_MAX_PROVIDER_REFUND_ID: Final = 64

#: Version tag mixed into every idempotency key and case key. A changed derivation gets a
#: new tag rather than a key that collides with a historical one under the old rule.
_KEY_SCHEME_VERSION: Final = 1

#: Prefix shared with the adapter's key derivation so a key is recognisable wherever it
#: appears -- a Razorpay dashboard, an outbox row, a refunds row.
_KEY_PREFIX: Final = "rfnd_"
_CASE_KEY_PREFIX: Final = "hrc_"


class RefundStatus(StrEnum):
    """Mirrors the ``status_enum`` check constraint on ``refunds``."""

    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    ESCALATED = "ESCALATED"


#: Statuses whose amount counts against the capture. Everything except a provider-confirmed
#: failure: the money is either gone or may be gone, and either way it is not available to
#: refund again.
RESERVING_STATUSES: Final[frozenset[RefundStatus]] = frozenset(
    {
        RefundStatus.PENDING,
        RefundStatus.PROCESSED,
        RefundStatus.UNKNOWN,
        RefundStatus.RECONCILING,
        RefundStatus.ESCALATED,
    }
)

RefundOutcome = Literal["processed", "pending", "failed", "unknown"]
VerifiedRefundState = Literal["exists_processed", "exists_pending", "absent"]
NextAction = Literal["none", "await_provider", "admit_new_refund", "escalate"]

_OUTCOMES: Final[frozenset[str]] = frozenset({"processed", "pending", "failed", "unknown"})
_VERIFIED: Final[frozenset[str]] = frozenset({"exists_processed", "exists_pending", "absent"})

#: Attempt states from which a refund may be escalated. ``REFUND_UNKNOWN`` reaches
#: ``ESCALATED`` through ``RECONCILING``; the other two have a direct edge.
_ESCALATABLE_STATES: Final[frozenset[PaymentState]] = frozenset(
    {PaymentState.REFUND_UNKNOWN, PaymentState.RECONCILING, PaymentState.REFUND_FAILED}
)
_ESCALATABLE_REFUNDS: Final[frozenset[RefundStatus]] = frozenset(
    {RefundStatus.UNKNOWN, RefundStatus.RECONCILING, RefundStatus.FAILED}
)


# ---------------------------------------------------------------------------- failures


class RefundError(Exception):
    """A deterministic refusal from the refund service, carrying a recovery code."""

    code: RecoveryCode = RecoveryCode.POLICY_EXCEPTION

    def __init__(self, message: str, *, refund_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.refund_id = refund_id


class RefundUsageError(RuntimeError):
    """The caller used the API wrongly: no transaction, or a malformed argument.

    Not a :class:`RefundError` because there is no buyer-facing recovery from a kernel
    calling itself incorrectly; a code for it would put a programming bug into the
    agent's vocabulary.
    """


class RefundTenantMismatchError(RefundError):
    """The caller named a tenant other than the one bound to this transaction."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class RefundNotFoundError(RefundError):
    """No such refund (or attempt) is visible to the bound tenant. Under row-level
    security another tenant's row is indistinguishable from one that never existed."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class RefundStateError(RefundError):
    """The refund or its attempt is in a state this operation may not act on."""

    code = RecoveryCode.POLICY_EXCEPTION


class RefundLedgerError(RefundError):
    """The provider's figures cannot be reconciled with the local capture ledger."""

    code = RecoveryCode.HUMAN_REVIEW_REQUIRED


# ------------------------------------------------------------------------------ values


@dataclass(frozen=True, slots=True)
class RefundLedger:
    """What one capture can still refund, from committed rows.

    ``reserved`` is every refund in :data:`RESERVING_STATUSES`; ``settled`` is the
    ``PROCESSED`` subset. ``next_sequence`` is the 1-based ordinal the next refund row
    takes, counted over every row of any status so a retry after ``FAILED`` gets a new
    idempotency key rather than the failed refund's.
    """

    captured: Money
    reserved: Money
    settled: Money
    next_sequence: int

    @property
    def remaining(self) -> Money:
        """The largest refund this capture can still support."""
        return self.captured - self.reserved

    @property
    def fully_settled(self) -> bool:
        return self.settled == self.captured


@dataclass(frozen=True, slots=True)
class RefundAdmission:
    """The answer to "may this refund be attempted".

    ``decision`` is always present. On success it is allowed, carries the grant, and the
    other fields name the refund row and the key the worker must send. On a denial the
    other fields are ``None`` -- except when an earlier, identical admission is returned
    (``DUPLICATE_OPERATION``), where they name that earlier refund and its grant.
    """

    refund_id: uuid.UUID | None
    grant_id: uuid.UUID | None
    amount: Money | None
    sequence: int | None
    idem_key: str | None
    decision: AdmissionDecision

    @property
    def allowed(self) -> bool:
        return self.decision.allowed


@dataclass(frozen=True, slots=True)
class RefundTransition:
    """What one recorded outcome did to the refund row and the attempt."""

    refund_id: uuid.UUID
    payment_attempt_id: uuid.UUID
    refund_status_before: RefundStatus
    refund_status_after: RefundStatus
    attempt_state_before: PaymentState
    attempt_state_after: PaymentState
    changed: bool
    code: RecoveryCode
    explanation: str


@dataclass(frozen=True, slots=True)
class RefundReconciliation:
    """What verified provider evidence did, and what the caller must do next.

    ``next_action`` is the contract: ``admit_new_refund`` means the provider verified the
    refund is absent and the caller may call :func:`admit_refund` again for a new row and
    a new grant; ``await_provider`` means the refund exists but has not settled;
    ``escalate`` means the evidence contradicts the record and a person must look.
    """

    refund_id: uuid.UUID
    payment_attempt_id: uuid.UUID
    refund_status_before: RefundStatus
    refund_status_after: RefundStatus
    attempt_state_before: PaymentState
    attempt_state_after: PaymentState
    next_action: NextAction
    code: RecoveryCode
    explanation: str


@dataclass(frozen=True, slots=True)
class RefundEscalation:
    """The frozen attempt and the one human-review case it opened (or already had)."""

    refund_id: uuid.UUID
    payment_attempt_id: uuid.UUID
    case_key: str
    opened: bool
    attempt_state_before: PaymentState
    attempt_state_after: PaymentState
    code: RecoveryCode = RecoveryCode.HUMAN_REVIEW_REQUIRED


# ------------------------------------------------------------------------------- keys


def refund_idempotency_key(
    *, tenant_id: uuid.UUID, payment_attempt_id: uuid.UUID, sequence: int, amount: Money
) -> str:
    """The stable ``X-Refund-Idempotency`` for one refund row.

    Stable across every transport retry of the same refund (it is stored on the row and
    the worker sends the stored value), distinct between two refunds of the same amount
    on the same attempt because ``sequence`` is the row's ordinal in the attempt's
    committed refund history. Keyed on the attempt rather than the provider payment id
    so the key exists at admission, before any provider identifier is known.
    """
    if sequence < 1:
        raise RefundUsageError(f"refund sequence must be a 1-based ordinal, got {sequence}")
    digest = canonical_hash(
        {
            "v": _KEY_SCHEME_VERSION,
            "tenant_id": str(tenant_id),
            "payment_attempt_id": str(payment_attempt_id),
            "sequence": sequence,
            "amount_minor": amount.minor,
            "currency": amount.currency,
        }
    )
    return f"{_KEY_PREFIX}{digest}"


def human_review_case_key(
    *, tenant_id: uuid.UUID, order_ref: uuid.UUID, subject_id: uuid.UUID, reason_family: str
) -> str:
    """Specification 6.4.3: ``tenant_id + order_id + payment_or_refund_id + reason_family``.

    ``order_ref`` is the checkout id, which every attempt has even before an ``orders``
    row exists; ``subject_id`` is the refund id here and the payment attempt id on the
    payment path. Deterministic so three detectors of one stuck refund derive one key.
    """
    if not reason_family or len(reason_family) > _MAX_REASON_CODE:
        raise RefundUsageError("reason_family must be a non-empty key of at most 64 characters")
    digest = canonical_hash(
        {
            "v": _KEY_SCHEME_VERSION,
            "tenant_id": str(tenant_id),
            "order_ref": str(order_ref),
            "subject_id": str(subject_id),
            "reason_family": reason_family,
        }
    )
    return f"{_CASE_KEY_PREFIX}{digest}"


# ------------------------------------------------------------------------- row access
#
# Every SQL string below is composed from literals in this module; every variable travels
# as a bound parameter. tenant_id is repeated in each predicate beside row-level security
# so a dropped policy cannot turn a refund id into a cross-tenant capability.


@dataclass(frozen=True, slots=True)
class _Attempt:
    id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    content_hash: str
    status: PaymentState
    amount: Money
    provider_payment_id: str | None

    @property
    def checkout(self) -> CheckoutRef:
        return CheckoutRef(self.checkout_id, self.checkout_version, self.content_hash)


@dataclass(frozen=True, slots=True)
class _RefundRow:
    id: uuid.UUID
    payment_attempt_id: uuid.UUID
    checkout_id: uuid.UUID
    status: RefundStatus
    amount: Money
    idem_key: str
    provider_refund_id: str | None
    provider_originated: bool
    reason_code: str


_SELECT_ATTEMPT_HEAD = text(
    "SELECT checkout_id, checkout_version FROM payment_attempts WHERE tenant_id = :t AND id = :id"
)
_LOCK_CHECKOUT_VERSION = text(
    "SELECT content_hash FROM checkout_versions "
    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v FOR UPDATE"
)
_LOCK_ATTEMPT = text(
    "SELECT id, checkout_id, checkout_version, status, amount_minor, currency, "
    "provider_payment_id FROM payment_attempts WHERE tenant_id = :t AND id = :id FOR UPDATE"
)
_SELECT_REFUND_HEAD = text(
    "SELECT payment_attempt_id FROM refunds WHERE tenant_id = :t AND id = :id"
)
# The refund column list is repeated literally in each statement below rather than
# interpolated, so every statement is a constant the linter can see whole.
_LOCK_REFUND = text(
    "SELECT id, payment_attempt_id, checkout_id, status, amount_minor, currency, idem_key, "
    "provider_refund_id, provider_originated, reason_code FROM refunds "
    "WHERE tenant_id = :t AND id = :id FOR UPDATE"
)
_SELECT_LEDGER = text(
    "SELECT status, amount_minor, currency FROM refunds "
    "WHERE tenant_id = :t AND payment_attempt_id = :a"
)
_SELECT_LIVE_GRANT = text(
    "SELECT id FROM execution_grants WHERE tenant_id = :t AND payment_attempt_id = :a "
    "AND status = 'ISSUED' LIMIT 1"
)
_SELECT_GRANT_FOR_REFUND = text(
    "SELECT id FROM execution_grants WHERE tenant_id = :t AND refund_id = :r "
    "ORDER BY issued_at DESC LIMIT 1"
)
_SELECT_STALE_REFUND = text(
    "SELECT id, payment_attempt_id, checkout_id, status, amount_minor, currency, idem_key, "
    "provider_refund_id, provider_originated, reason_code FROM refunds "
    "WHERE tenant_id = :t AND payment_attempt_id = :a AND reason_code = :reason "
    "AND status <> 'FAILED' ORDER BY created_at LIMIT 1"
)
_SELECT_BY_PROVIDER_ID = text(
    "SELECT id, payment_attempt_id, checkout_id, status, amount_minor, currency, idem_key, "
    "provider_refund_id, provider_originated, reason_code FROM refunds "
    "WHERE tenant_id = :t AND payment_attempt_id = :a AND provider_refund_id = :pid LIMIT 1"
)
_COUNT_UNRESOLVED_LOCAL = text(
    "SELECT count(*) FROM refunds WHERE tenant_id = :t AND payment_attempt_id = :a "
    "AND provider_refund_id IS NULL AND amount_minor = :amt "
    "AND status IN ('PENDING','UNKNOWN','RECONCILING')"
)
_INSERT_REFUND = text(
    "INSERT INTO refunds (id, tenant_id, payment_attempt_id, order_id, checkout_id, status, "
    "amount_minor, currency, idem_key, provider_refund_id, provider_originated, reason_code) "
    "VALUES (:id, :t, :a, :o, :c, :status, :amt, :cur, :key, :pid, :ext, :reason)"
)

#: The order a capture produced, which is the order a refund against it returns money for.
_ORDER_OF_ATTEMPT = text("SELECT id FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a")
_UPDATE_REFUND = text(
    "UPDATE refunds SET status = :status, "
    "provider_refund_id = COALESCE(:pid, provider_refund_id), updated_at = now() "
    "WHERE tenant_id = :t AND id = :id"
)
_UPDATE_ATTEMPT = text(
    "UPDATE payment_attempts SET status = :status, updated_at = now() "
    "WHERE tenant_id = :t AND id = :id"
)


def _order_of(session: Session, tenant: uuid.UUID, attempt_id: uuid.UUID) -> uuid.UUID | None:
    """The order this attempt produced, when there is one.

    There is one on every ordinary path: an order is written at capture and a refund exists
    only against a capture. It is read rather than required because of the stale capture --
    money taken against a version invalidated while the payment was moving, for which no
    order is written because nothing was sold. The platform refunds that in full on its own,
    and that refund has no order to name. Demanding one would stop the kernel recording
    money it has already sent back.
    """
    found = session.execute(_ORDER_OF_ATTEMPT, {"t": tenant, "a": attempt_id}).scalar()
    return cast("uuid.UUID | None", found)


def _bound_tenant(session: Session, tenant_id: uuid.UUID) -> uuid.UUID:
    """Every entry point's preconditions: an open transaction and the bound tenant.

    ``tenant_id`` is an argument the caller could spoof; the GUC is what row-level
    security enforces. Requiring the two to agree keeps a cross-tenant call legible
    rather than letting RLS answer it with an empty result that reads as "not found".
    """
    if not session.in_transaction():
        raise RefundUsageError(
            "refund operations must run inside a transaction; their guarantees come from "
            "locks that only exist for the life of one"
        )
    if not isinstance(tenant_id, uuid.UUID):
        raise RefundUsageError(f"tenant_id must be a UUID, got {type(tenant_id).__name__}")
    if not isinstance(session, Session):
        raise RefundUsageError("session must be a SQLAlchemy Session")
    bound = require_tenant(session)
    if bound != tenant_id:
        raise RefundTenantMismatchError(
            f"tenant {tenant_id} is not the tenant bound to this transaction ({bound})"
        )
    return bound


def _lock_attempt(session: Session, tenant: uuid.UUID, attempt_id: uuid.UUID) -> _Attempt | None:
    """Locate, then lock, in the ADR 0003 D5 order.

    Plain SELECT to learn the checkout, ``checkout_versions FOR UPDATE``, then
    ``payment_attempts FOR UPDATE``. Cancel, provider-evidence application and refunds all
    take these two locks in this sequence, which is why they cannot deadlock one another.
    The status is read from the locked row, never from the locating read.
    """
    head = session.execute(_SELECT_ATTEMPT_HEAD, {"t": tenant, "id": attempt_id}).one_or_none()
    if head is None:
        return None
    version = session.execute(
        _LOCK_CHECKOUT_VERSION,
        {"t": tenant, "c": head.checkout_id, "v": head.checkout_version},
    ).one_or_none()
    if version is None:
        raise RefundStateError(
            f"payment attempt {attempt_id} names checkout version "
            f"{head.checkout_id}/{head.checkout_version}, which does not exist"
        )
    row = session.execute(_LOCK_ATTEMPT, {"t": tenant, "id": attempt_id}).one()
    return _Attempt(
        id=row.id,
        checkout_id=row.checkout_id,
        checkout_version=int(row.checkout_version),
        content_hash=str(version.content_hash),
        status=PaymentState(row.status),
        amount=Money(int(row.amount_minor), str(row.currency)),
        provider_payment_id=row.provider_payment_id,
    )


def _refund_view(row: Any) -> _RefundRow:
    return _RefundRow(
        id=row.id,
        payment_attempt_id=row.payment_attempt_id,
        checkout_id=row.checkout_id,
        status=RefundStatus(row.status),
        amount=Money(int(row.amount_minor), str(row.currency)),
        idem_key=str(row.idem_key),
        provider_refund_id=row.provider_refund_id,
        provider_originated=bool(row.provider_originated),
        reason_code=str(row.reason_code),
    )


def _attempt_of_refund(session: Session, tenant: uuid.UUID, refund_id: uuid.UUID) -> uuid.UUID:
    head = session.execute(_SELECT_REFUND_HEAD, {"t": tenant, "id": refund_id}).one_or_none()
    if head is None:
        raise RefundNotFoundError(f"no refund {refund_id} for this tenant", refund_id=refund_id)
    attempt_id: uuid.UUID = head.payment_attempt_id
    return attempt_id


def _lock_refund(session: Session, tenant: uuid.UUID, refund_id: uuid.UUID) -> _RefundRow:
    """``refunds FOR UPDATE`` -- last in the lock order, after the attempt and its grants."""
    row = session.execute(_LOCK_REFUND, {"t": tenant, "id": refund_id}).one_or_none()
    if row is None:
        raise RefundNotFoundError(f"no refund {refund_id} for this tenant", refund_id=refund_id)
    return _refund_view(row)


def _locked_pair(
    session: Session, tenant: uuid.UUID, refund_id: uuid.UUID
) -> tuple[_Attempt, _RefundRow]:
    attempt_id = _attempt_of_refund(session, tenant, refund_id)
    attempt = _lock_attempt(session, tenant, attempt_id)
    if attempt is None:  # pragma: no cover - the FK makes this unreachable
        raise RefundNotFoundError(f"attempt {attempt_id} of refund {refund_id} is missing")
    return attempt, _lock_refund(session, tenant, refund_id)


def _ledger(session: Session, tenant: uuid.UUID, attempt: _Attempt) -> RefundLedger:
    """Sum the attempt's committed refund rows. Called only under the attempt lock, so
    every writer of these rows is serialized behind this read."""
    currency = attempt.amount.currency
    reserved = Money.zero(currency)
    settled = Money.zero(currency)
    rows = 0
    for row in session.execute(_SELECT_LEDGER, {"t": tenant, "a": attempt.id}).all():
        rows += 1
        if str(row.currency) != currency:
            raise RefundLedgerError(
                f"refund row on attempt {attempt.id} is in {row.currency}, capture is in "
                f"{currency}; the ledger is inconsistent and needs a person, not a refund"
            )
        status = RefundStatus(row.status)
        amount = Money(int(row.amount_minor), currency)
        if status in RESERVING_STATUSES:
            reserved = reserved + amount
        if status is RefundStatus.PROCESSED:
            settled = settled + amount
    if reserved > attempt.amount:
        raise RefundLedgerError(
            f"refunds reserved on attempt {attempt.id} ({reserved.minor}) exceed the capture "
            f"({attempt.amount.minor}); the ledger is inconsistent and needs a person"
        )
    return RefundLedger(
        captured=attempt.amount, reserved=reserved, settled=settled, next_sequence=rows + 1
    )


def _set_refund_status(
    session: Session,
    tenant: uuid.UUID,
    refund_id: uuid.UUID,
    status: RefundStatus,
    *,
    provider_refund_id: str | None = None,
) -> None:
    session.execute(
        _UPDATE_REFUND,
        {"status": status.value, "pid": provider_refund_id, "t": tenant, "id": refund_id},
    )


def _edges_to(current: PaymentState, target: PaymentState) -> tuple[PaymentState, ...] | None:
    """The legal path from ``current`` to ``target`` in at most two declared edges.

    Two, not more: every refund move in this module is either a direct edge or one hop
    through ``REFUND_PENDING`` / ``RECONCILING``. A longer walk would be a route the
    lifecycle never intended, so it is reported as impossible rather than found.
    """
    if current is target:
        return ()
    successors = PAYMENT_TRANSITIONS[current]
    if target in successors:
        return (target,)
    for middle in sorted(successors, key=lambda state: state.value):
        if target in PAYMENT_TRANSITIONS[middle]:
            return (middle, target)
    return None


def _advance_attempt(
    session: Session, tenant: uuid.UUID, attempt: _Attempt, target: PaymentState
) -> tuple[PaymentState, ...]:
    """Move the locked attempt to ``target`` through declared edges only.

    Every hop goes through :func:`assert_transition`, so this cannot invent an edge; it
    can only chain two that exist. Returns the states written, in order.
    """
    route = _edges_to(attempt.status, target)
    if route is None:
        raise InvalidTransitionError(attempt.status, target)
    current = attempt.status
    for step in route:
        assert_transition(current, step)
        current = step
    if route:
        session.execute(_UPDATE_ATTEMPT, {"status": target.value, "t": tenant, "id": attempt.id})
    return route


def _system_principal(tenant: uuid.UUID, purpose: str) -> AgentPrincipal:
    return AgentPrincipal(
        principal_id=f"kernel:{purpose}", tenant_id=tenant, actor_type=ActorType.SYSTEM
    )


def _audit(
    session: Session,
    tenant: uuid.UUID,
    attempt_id: uuid.UUID,
    event_type: str,
    *,
    actor_type: ActorType,
    principal_id: str | None,
    payload: dict[str, Any],
    correlation_id: uuid.UUID,
) -> None:
    audit.append(
        session,
        tenant=tenant,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=attempt_id,
        event_type=event_type,
        actor_type=actor_type,
        principal_id=principal_id,
        payload=payload,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------- admission


def _deny(
    session: Session,
    tenant: uuid.UUID,
    *,
    attempt_id: uuid.UUID,
    checkout: CheckoutRef | None,
    principal: AgentPrincipal,
    correlation_id: uuid.UUID,
    code: RecoveryCode,
    explanation: str,
    deltas: Sequence[Delta] = (),
    reason_code: str,
    requested: Money | None,
) -> RefundAdmission:
    """Record a denial and return it. Refusing well is most of what this path does, and a
    refusal that left no evidence could not be explained to the buyer afterwards."""
    decision = AdmissionDecision(
        decision_id=uuid7(),
        allowed=False,
        code=code,
        explanation=explanation,
        checkout=checkout,
        deltas=tuple(deltas),
        payment_attempt_id=attempt_id,
        correlation_id=correlation_id,
    )
    _audit(
        session,
        tenant,
        attempt_id,
        "refund.denied",
        actor_type=principal.actor_type,
        principal_id=principal.principal_id,
        payload={
            "decision_id": decision.decision_id,
            "code": str(code),
            "explanation": explanation,
            "reason_code": reason_code,
            "requested": requested,
            "deltas": [
                {"field": d.field_path, "approved": d.approved, "current": d.current}
                for d in decision.deltas
            ],
        },
        correlation_id=correlation_id,
    )
    increment(
        session,
        tenant,
        "commerce_admissions_total",
        operation=Operation.REFUND_EXECUTE.value,
        outcome="allowed" if decision.allowed else "denied",
    )
    if not decision.allowed:
        increment(
            session,
            tenant,
            "commerce_admission_denials_total",
            operation=Operation.REFUND_EXECUTE.value,
            code=decision.code.value,
        )
    return RefundAdmission(
        refund_id=None, grant_id=None, amount=None, sequence=None, idem_key=None, decision=decision
    )


def _validate_admission_inputs(
    amount: Money | None, reason_code: str, principal: AgentPrincipal, tenant: uuid.UUID
) -> None:
    if amount is not None and not isinstance(amount, Money):
        raise RefundUsageError(f"amount must be Money or None, got {type(amount).__name__}")
    if not isinstance(reason_code, str) or not reason_code or len(reason_code) > _MAX_REASON_CODE:
        raise RefundUsageError("reason_code must be a non-empty key of at most 64 characters")
    if not isinstance(principal, AgentPrincipal):
        raise RefundUsageError(f"principal must be an AgentPrincipal, got {type(principal)}")
    if principal.tenant_id != tenant:
        raise RefundTenantMismatchError(
            f"principal belongs to tenant {principal.tenant_id}, transaction is bound to {tenant}"
        )


def _admit(
    session: Session,
    tenant: uuid.UUID,
    *,
    attempt: _Attempt,
    amount: Money | None,
    reason_code: str,
    principal: AgentPrincipal,
    correlation_id: uuid.UUID,
    provider_originated: bool,
    grant_ttl_seconds: int,
) -> RefundAdmission:
    """The admission proper, on an attempt this transaction already locked."""
    checkout = attempt.checkout
    state = attempt.status

    def deny(
        code: RecoveryCode, explanation: str, *, deltas: Sequence[Delta] = ()
    ) -> RefundAdmission:
        return _deny(
            session,
            tenant,
            attempt_id=attempt.id,
            checkout=checkout,
            principal=principal,
            correlation_id=correlation_id,
            code=code,
            explanation=explanation,
            deltas=deltas,
            reason_code=reason_code,
            requested=amount,
        )

    # --- one refund in flight per attempt; uncertainty reconciles, never re-admits ------
    if state in (PaymentState.REFUND_PENDING, PaymentState.AUTO_REFUND_PENDING):
        # The two-callers race lands here: the loser waited on the attempt lock, re-read
        # the winner's committed state, and is told to read that refund rather than
        # create a second. CONCURRENT_OPERATION, never DUPLICATE_OPERATION: nothing has
        # been confirmed yet, so nothing may be presented to the buyer as done.
        return deny(RecoveryCode.CONCURRENT_OPERATION, "refund_already_in_flight")
    if state in UNCERTAIN_PAYMENT_STATES or state is PaymentState.RECONCILING:
        return deny(RecoveryCode.RECONCILIATION_IN_PROGRESS, "reconcile_unknown_first")
    if not can_transition(state, PaymentState.REFUND_PENDING):
        return deny(RecoveryCode.POLICY_EXCEPTION, "state_forbids_refund")

    # --- the ledger, from locked and committed rows only ---------------------------------
    book = _ledger(session, tenant, attempt)
    if amount is not None and amount.currency != book.captured.currency:
        return deny(RecoveryCode.POLICY_EXCEPTION, "currency_mismatch")
    if amount is not None and amount.minor <= 0:
        return deny(RecoveryCode.POLICY_EXCEPTION, "non_positive_amount")
    remaining = book.remaining
    if remaining.is_zero:
        return deny(RecoveryCode.POLICY_EXCEPTION, "nothing_remaining")
    requested = remaining if amount is None else amount
    if requested > remaining:
        return deny(
            RecoveryCode.POLICY_EXCEPTION,
            "exceeds_remaining",
            deltas=(
                Delta(
                    field_path="amount_minor",
                    approved=remaining.minor,
                    current=requested.minor,
                    reason="EXCEEDS_REMAINING",
                ),
            ),
        )
    if state is PaymentState.STALE_CAPTURE and requested != remaining:
        # Specification 10.8: a stale capture is refunded in full, never in part.
        return deny(RecoveryCode.POLICY_EXCEPTION, "stale_capture_requires_full_refund")

    # --- the terms this sale was made under, which no later policy may narrow ---------
    #
    # The Policy-at-Sale Receipt has always recorded a refund window and the kernel has
    # always proved that record immutable. This is where the record starts governing
    # anything: without it a merchant's window was a number nothing read, and a refund on
    # day three and on day three hundred were admitted identically.
    #
    # Two admissions are deliberately not gated, and neither is a hole:
    #
    # * A stale capture is the platform returning money it took against a checkout that
    #   was no longer valid (specification 10.8). That is the platform's own error being
    #   corrected, not a buyer claiming under merchant terms, and a merchant's window has
    #   no business barring it.
    # * ``provider_originated`` would record a refund Razorpay has already started. The
    #   money is moving whatever the terms say; refusing to write it down would leave the
    #   ledger disagreeing with the provider, which is the one outcome reconciliation
    #   exists to prevent. **No caller sets it today** -- a dashboard refund is written by
    #   ``record_provider_originated_refund``, which does not come through here at all --
    #   so this arm is unreached rather than merely rare. It stays because the flag is on
    #   this function's public signature: the day something does pass it, being barred by
    #   a merchant's window is the wrong answer, and the rule is cheaper to state now than
    #   to rediscover then.
    #
    # After the ledger, not before it. "Nothing is left to refund" is a fact about money
    # that has already gone back and is the truer answer when both apply; a buyer whose
    # refund already settled should not be told they were too late.
    if state is not PaymentState.STALE_CAPTURE and not provider_originated:
        window = refund_window_at_sale(session, checkout, now_ms=database_now_ms(session))
        if window.closed:
            return deny(window.code, window.explanation, deltas=window.deltas)
        if not window.partial_allowed and requested != remaining:
            return deny(RecoveryCode.POLICY_EXCEPTION, "partial_refund_not_offered")

    # --- the one live grant per attempt, answered here rather than by the index -----------
    live = session.execute(_SELECT_LIVE_GRANT, {"t": tenant, "a": attempt.id}).one_or_none()
    if live is not None:
        return deny(RecoveryCode.CONCURRENT_OPERATION, "grant_already_live")

    # --- exactly one refund row and exactly one grant --------------------------------
    sequence = book.next_sequence
    idem_key = refund_idempotency_key(
        tenant_id=tenant, payment_attempt_id=attempt.id, sequence=sequence, amount=requested
    )
    refund_id = uuid7()
    decision_id = uuid7()
    session.execute(
        _INSERT_REFUND,
        {
            "id": refund_id,
            "t": tenant,
            "a": attempt.id,
            "o": _order_of(session, tenant, attempt.id),
            "c": attempt.checkout_id,
            "status": RefundStatus.PENDING.value,
            "amt": requested.minor,
            "cur": requested.currency,
            "key": idem_key,
            "pid": None,
            "ext": provider_originated,
            "reason": reason_code,
        },
    )
    grant = grants.issue_grant(
        session,
        tenant=tenant,
        checkout_ref=checkout,
        payment_attempt_id=attempt.id,
        operation=Operation.REFUND_EXECUTE,
        amount=requested,
        kernel_decision_id=decision_id,
        ttl_seconds=grant_ttl_seconds,
        refund_id=refund_id,
    )
    route = _advance_attempt(session, tenant, attempt, PaymentState.REFUND_PENDING)

    decision = AdmissionDecision(
        decision_id=decision_id,
        allowed=True,
        code=RecoveryCode.OK,
        explanation="refund_admitted",
        checkout=checkout,
        grant_id=grant.id,
        payment_attempt_id=attempt.id,
        correlation_id=correlation_id,
    )
    _audit(
        session,
        tenant,
        attempt.id,
        "refund.admitted",
        actor_type=principal.actor_type,
        principal_id=principal.principal_id,
        payload={
            "decision_id": decision_id,
            "refund_id": refund_id,
            "grant_id": grant.id,
            "amount": requested,
            "sequence": sequence,
            "idem_key": idem_key,
            "reason_code": reason_code,
            "provider_originated": provider_originated,
            "captured": book.captured,
            "reserved_before": book.reserved,
            "remaining_after": remaining - requested,
            "is_full_remaining": requested == remaining,
            "state_before": str(state),
            "state_after": str(route[-1]) if route else str(state),
            "checkout_version": checkout.version,
            "content_hash": checkout.content_hash,
        },
        correlation_id=correlation_id,
    )
    increment(
        session,
        tenant,
        "commerce_admissions_total",
        operation=Operation.REFUND_EXECUTE.value,
        outcome="allowed" if decision.allowed else "denied",
    )
    if not decision.allowed:
        increment(
            session,
            tenant,
            "commerce_admission_denials_total",
            operation=Operation.REFUND_EXECUTE.value,
            code=decision.code.value,
        )
    return RefundAdmission(
        refund_id=refund_id,
        grant_id=grant.id,
        amount=requested,
        sequence=sequence,
        idem_key=idem_key,
        decision=decision,
    )


def admit_refund(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    amount: Money | None,
    reason_code: str,
    principal: AgentPrincipal,
    correlation_id: uuid.UUID,
    provider_originated: bool = False,
    grant_ttl_seconds: int = REFUND_GRANT_TTL_SECONDS,
) -> RefundAdmission:
    """Admit one refund against a captured attempt, or deny it with a structured reason.

    ``amount=None`` asks for everything still refundable and is resolved to that exact
    figure, so the grant and the row say what they mean.

    Guarantees on success: one ``PENDING`` refunds row with a stable idempotency key, one
    ``REFUND_EXECUTE`` grant bound to that row (ADR 0003 D10), the attempt moved to
    ``REFUND_PENDING`` through a declared edge, and a ``refund.admitted`` event in the same
    transaction. The amount never exceeds ``captured - reserved``, where reserved counts
    every refund that may exist at the provider (see the module docstring).

    Denials (``decision.allowed`` is False, ``refund.denied`` is audited):

    * ``CONCURRENT_OPERATION`` -- a refund is already in flight on this attempt, or a grant
      is still live on it. The second of two concurrent callers gets this.
    * ``RECONCILIATION_IN_PROGRESS`` -- the attempt is ``REFUND_UNKNOWN``, ``UNKNOWN`` or
      ``RECONCILING``; a refund may already exist and only reconciliation may say.
    * ``POLICY_EXCEPTION`` -- nothing remaining, amount exceeds remaining (with a delta
      naming both figures), wrong currency, non-positive amount, a partial refund of a
      stale capture, or a state with no refund edge (``AUTHORIZED``, ``FAILED``, ...).
    * ``AUTHORITY_INSUFFICIENT`` -- no such attempt for this tenant.
    * ``SAFE_MODE_ACTIVE`` -- never today: refunds are on Safe Mode's permitted list,
      and the gate is consulted anyway so that classification, not this module, decides.

    ``REFUND_FAILED`` is admissible: the provider confirmed nothing exists, so this mints a
    new row and a new grant. ``provider_originated`` marks a refund the platform is
    admitting on behalf of one the provider already started; it changes no arithmetic.

    Raises :class:`RefundUsageError` for a malformed call and
    :class:`RefundTenantMismatchError` when ``tenant_id`` or the principal's tenant is not
    the tenant bound to this transaction. Never commits and never rolls back.
    """
    tenant = _bound_tenant(session, tenant_id)
    _validate_admission_inputs(amount, reason_code, principal, tenant)
    if not isinstance(correlation_id, uuid.UUID):
        raise RefundUsageError("correlation_id must be a UUID")

    permitted, mode_code = safe_mode.is_permitted(
        session, tenant, safe_mode.GuardedOperation.REFUND_EXECUTE
    )
    attempt = _lock_attempt(session, tenant, payment_attempt_id)
    if attempt is None:
        return _deny(
            session,
            tenant,
            attempt_id=payment_attempt_id,
            checkout=None,
            principal=principal,
            correlation_id=correlation_id,
            code=RecoveryCode.AUTHORITY_INSUFFICIENT,
            explanation="payment_attempt_not_found",
            reason_code=reason_code,
            requested=amount,
        )
    if not permitted:  # pragma: no cover - REFUND_EXECUTE is on the permitted list
        return _deny(
            session,
            tenant,
            attempt_id=attempt.id,
            checkout=attempt.checkout,
            principal=principal,
            correlation_id=correlation_id,
            code=mode_code,
            explanation="safe_mode_blocks_operation",
            reason_code=reason_code,
            requested=amount,
        )
    return _admit(
        session,
        tenant,
        attempt=attempt,
        amount=amount,
        reason_code=reason_code,
        principal=principal,
        correlation_id=correlation_id,
        provider_originated=provider_originated,
        grant_ttl_seconds=grant_ttl_seconds,
    )


def admit_stale_capture_refund(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
    grant_ttl_seconds: int = REFUND_GRANT_TTL_SECONDS,
) -> RefundAdmission:
    """The automatic full refund of a capture against an invalidated checkout (10.8).

    Idempotent: if a ``STALE_CAPTURE`` refund already exists on the attempt in any status
    but ``FAILED``, it is returned -- ``decision.code`` is ``DUPLICATE_OPERATION`` and the
    refund and grant fields name the existing row and its grant -- and nothing is written.
    Two workers applying the same late-capture webhook therefore produce one refund. A
    ``FAILED`` stale refund is not reused: the provider confirmed nothing exists, so a new
    row and a new grant are admitted.

    Otherwise the attempt must be ``STALE_CAPTURE`` (``POLICY_EXCEPTION``,
    ``not_a_stale_capture`` if not) and the refund is admitted for the full remaining
    amount under the ``SYSTEM`` actor.
    """
    tenant = _bound_tenant(session, tenant_id)
    if not isinstance(correlation_id, uuid.UUID):
        raise RefundUsageError("correlation_id must be a UUID")
    principal = _system_principal(tenant, "stale_capture")

    permitted, mode_code = safe_mode.is_permitted(
        session, tenant, safe_mode.GuardedOperation.REFUND_EXECUTE
    )
    attempt = _lock_attempt(session, tenant, payment_attempt_id)
    if attempt is None:
        return _deny(
            session,
            tenant,
            attempt_id=payment_attempt_id,
            checkout=None,
            principal=principal,
            correlation_id=correlation_id,
            code=RecoveryCode.AUTHORITY_INSUFFICIENT,
            explanation="payment_attempt_not_found",
            reason_code=STALE_CAPTURE_REASON,
            requested=None,
        )

    existing = session.execute(
        _SELECT_STALE_REFUND, {"t": tenant, "a": attempt.id, "reason": STALE_CAPTURE_REASON}
    ).one_or_none()
    if existing is not None:
        row = _refund_view(existing)
        grant = session.execute(_SELECT_GRANT_FOR_REFUND, {"t": tenant, "r": row.id}).one_or_none()
        grant_id: uuid.UUID | None = None if grant is None else grant.id
        decision = AdmissionDecision(
            decision_id=uuid7(),
            allowed=False,
            code=RecoveryCode.DUPLICATE_OPERATION,
            explanation="stale_capture_refund_already_admitted",
            checkout=attempt.checkout,
            grant_id=grant_id,
            payment_attempt_id=attempt.id,
            correlation_id=correlation_id,
        )
        return RefundAdmission(
            refund_id=row.id,
            grant_id=grant_id,
            amount=row.amount,
            sequence=None,
            idem_key=row.idem_key,
            decision=decision,
        )

    if not permitted:  # pragma: no cover - REFUND_EXECUTE is on the permitted list
        return _deny(
            session,
            tenant,
            attempt_id=attempt.id,
            checkout=attempt.checkout,
            principal=principal,
            correlation_id=correlation_id,
            code=mode_code,
            explanation="safe_mode_blocks_operation",
            reason_code=STALE_CAPTURE_REASON,
            requested=None,
        )
    if attempt.status is not PaymentState.STALE_CAPTURE:
        return _deny(
            session,
            tenant,
            attempt_id=attempt.id,
            checkout=attempt.checkout,
            principal=principal,
            correlation_id=correlation_id,
            code=RecoveryCode.POLICY_EXCEPTION,
            explanation="not_a_stale_capture",
            reason_code=STALE_CAPTURE_REASON,
            requested=None,
        )
    return _admit(
        session,
        tenant,
        attempt=attempt,
        amount=None,
        reason_code=STALE_CAPTURE_REASON,
        principal=principal,
        correlation_id=correlation_id,
        provider_originated=False,
        grant_ttl_seconds=grant_ttl_seconds,
    )


# ------------------------------------------------------------------------------ results


def _check_provider_id(provider_refund_id: str | None, *, required: bool) -> str | None:
    if provider_refund_id is None:
        if required:
            raise RefundUsageError("a processed or existing refund must carry its provider id")
        return None
    if (
        not isinstance(provider_refund_id, str)
        or not provider_refund_id.strip()
        or len(provider_refund_id) > _MAX_PROVIDER_REFUND_ID
    ):
        raise RefundUsageError("provider_refund_id must be a non-empty string of at most 64 chars")
    return provider_refund_id


def _settled_target(session: Session, tenant: uuid.UUID, attempt: _Attempt) -> PaymentState:
    """``REFUNDED`` when every captured paisa is processed, else ``PARTIALLY_REFUNDED``.

    Re-read after the refund row was updated so the answer is a fact about the ledger,
    not about the one refund that just settled.
    """
    book = _ledger(session, tenant, attempt)
    return PaymentState.REFUNDED if book.fully_settled else PaymentState.PARTIALLY_REFUNDED


#: The attempt states that mean the order beneath them has been refunded, and what the
#: ``orders`` row should say when they are reached.
#:
#: Two rows describe one sale from different sides: the attempt is about a payment, the
#: order is about what the buyer bought. They are separate on purpose -- an order outlives
#: a payment being reconciled, and can be blocked for reasons no payment knows about --
#: but they must not contradict each other, and they did.
_ORDER_AFTER: Final[dict[PaymentState, str]] = {
    PaymentState.REFUNDED: "REFUNDED",
    PaymentState.PARTIALLY_REFUNDED: "PARTIALLY_REFUNDED",
}

_SETTLE_ORDER = text(
    "UPDATE orders SET status = :status, updated_at = now() "
    "WHERE tenant_id = :t AND payment_attempt_id = :a AND status <> :status"
)


def _settle_order(
    session: Session, tenant: uuid.UUID, attempt: _Attempt, state: PaymentState
) -> None:
    """Move the order beneath a settled refund, so the two rows agree.

    ``orders.status`` allows five values and nothing had ever written a second one. Every
    order said CONFIRMED from the moment it was created until forever, including after its
    money had gone back in full -- so the buyer's own screen showed a green badge and a
    delivery strip reading "payment confirmed" over a fully refunded sale, and four of the
    five states the schema allows were unreachable.

    The attempt has always moved. This is the row that did not, and the disagreement was
    invisible because CONFIRMED is the reassuring answer: nothing looked broken, it simply
    was not true.

    Silent where there is no order, because a stale capture is refunded without one
    (:func:`_order_of` says why), and silent on a state that settles nothing -- a refund
    going PENDING or UNKNOWN has decided nothing about what the buyer bought.
    """
    status = _ORDER_AFTER.get(state)
    if status is None:
        return
    session.execute(_SETTLE_ORDER, {"t": tenant, "a": attempt.id, "status": status})


def record_refund_result(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    outcome: RefundOutcome,
    provider_refund_id: str | None,
    correlation_id: uuid.UUID,
) -> RefundTransition:
    """Record what the provider said when the worker sent a ``PENDING`` refund.

    * ``processed`` -- the row becomes ``PROCESSED`` (``provider_refund_id`` required) and
      the attempt ``REFUNDED`` when the processed total equals the capture, otherwise
      ``PARTIALLY_REFUNDED``. Code ``OK``.
    * ``failed`` -- ``FAILED`` / ``REFUND_FAILED``; a retry is a fresh
      :func:`admit_refund`. Code ``PAYMENT_FAILED``.
    * ``unknown`` -- ``UNKNOWN`` / ``REFUND_UNKNOWN``; the only exit is
      :func:`reconcile_refund`. Code ``PAYMENT_UNKNOWN``.
    * ``pending`` -- nothing moves; a provider id, if given, is recorded. Code
      ``PAYMENT_PENDING``.

    A redelivered result on a row already in the outcome's status is a no-op
    (``changed`` False), so the worker can safely re-run a completed command. Any other
    result on a row that is not ``PENDING`` raises :class:`RefundStateError`: an
    ``UNKNOWN`` refund is resolved by reconciliation, not by a second report, and a
    settled one is never rewritten. Every call appends ``refund.result``.
    """
    tenant = _bound_tenant(session, tenant_id)
    if outcome not in _OUTCOMES:
        raise RefundUsageError(f"outcome must be one of {sorted(_OUTCOMES)}, got {outcome!r}")
    if not isinstance(correlation_id, uuid.UUID):
        raise RefundUsageError("correlation_id must be a UUID")
    provider_id = _check_provider_id(provider_refund_id, required=outcome == "processed")

    attempt, refund = _locked_pair(session, tenant, refund_id)
    before, state = refund.status, attempt.status
    target_status = {
        "processed": RefundStatus.PROCESSED,
        "failed": RefundStatus.FAILED,
        "unknown": RefundStatus.UNKNOWN,
        "pending": RefundStatus.PENDING,
    }[outcome]
    code = {
        "processed": RecoveryCode.OK,
        "failed": RecoveryCode.PAYMENT_FAILED,
        "unknown": RecoveryCode.PAYMENT_UNKNOWN,
        "pending": RecoveryCode.PAYMENT_PENDING,
    }[outcome]

    changed = False
    explanation: str
    new_state = state
    if before is target_status:
        # Duplicate delivery of a result already applied (or a pending report on a
        # pending row). The provider id is kept if it is new; nothing else moves.
        explanation = "already_recorded" if outcome != "pending" else "still_pending"
        if provider_id is not None and refund.provider_refund_id is None:
            _set_refund_status(session, tenant, refund.id, before, provider_refund_id=provider_id)
    elif before is not RefundStatus.PENDING:
        raise RefundStateError(
            f"refund {refund_id} is {before}; a provider result is recorded on a PENDING "
            "refund only (an UNKNOWN refund is resolved by reconcile_refund)",
            refund_id=refund_id,
        )
    else:
        changed = True
        explanation = f"refund_{outcome}"
        _set_refund_status(
            session, tenant, refund.id, target_status, provider_refund_id=provider_id
        )
        if outcome == "processed":
            target = _settled_target(session, tenant, attempt)
        elif outcome == "failed":
            target = PaymentState.REFUND_FAILED
        else:
            target = PaymentState.REFUND_UNKNOWN
        route = _advance_attempt(session, tenant, attempt, target)
        _settle_order(session, tenant, attempt, target)
        new_state = route[-1] if route else state

    _audit(
        session,
        tenant,
        attempt.id,
        "refund.result",
        actor_type=ActorType.WORKER,
        principal_id=None,
        payload={
            "refund_id": refund.id,
            "outcome": outcome,
            "explanation": explanation,
            "provider_refund_id": provider_id or refund.provider_refund_id,
            "amount": refund.amount,
            "refund_status_before": str(before),
            "refund_status_after": str(target_status if changed else before),
            "state_before": str(state),
            "state_after": str(new_state),
            "changed": changed,
        },
        correlation_id=correlation_id,
    )
    return RefundTransition(
        refund_id=refund.id,
        payment_attempt_id=attempt.id,
        refund_status_before=before,
        refund_status_after=target_status if changed else before,
        attempt_state_before=state,
        attempt_state_after=new_state,
        changed=changed,
        code=code,
        explanation=explanation,
    )


# ------------------------------------------------------------------------ reconciliation


def reconcile_refund(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    verified: VerifiedRefundState,
    provider_refund_id: str | None,
    correlation_id: uuid.UUID,
    attempt_number: int | None = None,
) -> RefundReconciliation:
    """Apply verified provider evidence to an ``UNKNOWN`` refund. The only exit from
    ``REFUND_UNKNOWN`` (specification 10.6), and it never issues a grant.

    * ``exists_processed`` -- the refund landed. Row ``PROCESSED`` (``provider_refund_id``
      required); attempt ``REFUNDED`` or ``PARTIALLY_REFUNDED`` by the ledger, via
      ``RECONCILING``. ``next_action`` ``none``.
    * ``exists_pending`` -- the refund exists but has not settled. Row ``RECONCILING``
      with the provider id recorded; attempt ``RECONCILING``. ``next_action``
      ``await_provider``: the caller re-enqueues with ``attempt_number + 1`` and, past
      :data:`MAX_RECONCILIATION_ATTEMPTS`, calls :func:`escalate_refund`.
    * ``absent`` -- the provider verified no refund exists. Row ``FAILED``; attempt
      ``REFUND_FAILED``. ``next_action`` ``admit_new_refund``: what the caller must do is
      a fresh :func:`admit_refund`, which mints a **new** row and a **new** grant. The
      consumed grant of this row is never re-armed.

    A refund the provider already confirmed exists (row ``RECONCILING``) that is now
    reported ``absent`` is a contradiction: nothing moves, ``next_action`` is ``escalate``
    with ``HUMAN_REVIEW_REQUIRED``. A ``PROCESSED`` row reported ``exists_processed`` is
    a no-op replay. Any other row status raises :class:`RefundStateError`.
    """
    tenant = _bound_tenant(session, tenant_id)
    if verified not in _VERIFIED:
        raise RefundUsageError(f"verified must be one of {sorted(_VERIFIED)}, got {verified!r}")
    if not isinstance(correlation_id, uuid.UUID):
        raise RefundUsageError("correlation_id must be a UUID")
    if attempt_number is not None and (isinstance(attempt_number, bool) or attempt_number < 1):
        raise RefundUsageError("attempt_number must be a positive int when given")
    provider_id = _check_provider_id(provider_refund_id, required=verified != "absent")

    attempt, refund = _locked_pair(session, tenant, refund_id)
    before, state = refund.status, attempt.status
    after, new_state = before, state
    next_action: NextAction
    code: RecoveryCode
    explanation: str

    if before is RefundStatus.PROCESSED and verified == "exists_processed":
        next_action, code, explanation = "none", RecoveryCode.OK, "already_processed"
    elif before not in (RefundStatus.UNKNOWN, RefundStatus.RECONCILING):
        raise RefundStateError(
            f"refund {refund_id} is {before}; only an UNKNOWN or RECONCILING refund is reconciled",
            refund_id=refund_id,
        )
    elif state not in (PaymentState.REFUND_UNKNOWN, PaymentState.RECONCILING):
        raise RefundStateError(
            f"attempt {attempt.id} is {state} while refund {refund_id} is {before}; "
            "reconciliation applies to REFUND_UNKNOWN or RECONCILING attempts only",
            refund_id=refund_id,
        )
    elif verified == "exists_processed":
        after = RefundStatus.PROCESSED
        _set_refund_status(session, tenant, refund.id, after, provider_refund_id=provider_id)
        settled = _settled_target(session, tenant, attempt)
        route = _advance_attempt(session, tenant, attempt, settled)
        _settle_order(session, tenant, attempt, settled)
        new_state = route[-1] if route else state
        next_action, code, explanation = "none", RecoveryCode.OK, "verified_refund_exists"
    elif verified == "exists_pending":
        after = RefundStatus.RECONCILING
        _set_refund_status(session, tenant, refund.id, after, provider_refund_id=provider_id)
        route = _advance_attempt(session, tenant, attempt, PaymentState.RECONCILING)
        new_state = route[-1] if route else state
        next_action = "await_provider"
        code, explanation = RecoveryCode.RECONCILIATION_IN_PROGRESS, "verified_refund_pending"
    elif before is RefundStatus.RECONCILING:
        # The provider said the refund existed on an earlier round and now says it does
        # not. Refunds do not vanish; the record is contradicted and a person decides.
        next_action = "escalate"
        code, explanation = RecoveryCode.HUMAN_REVIEW_REQUIRED, "contradictory_evidence"
    else:
        after = RefundStatus.FAILED
        _set_refund_status(session, tenant, refund.id, after)
        route = _advance_attempt(session, tenant, attempt, PaymentState.REFUND_FAILED)
        new_state = route[-1] if route else state
        next_action = "admit_new_refund"
        code, explanation = RecoveryCode.PAYMENT_FAILED, "verified_refund_absent"

    _audit(
        session,
        tenant,
        attempt.id,
        "refund.reconciled",
        actor_type=ActorType.WORKER,
        principal_id=None,
        payload={
            "refund_id": refund.id,
            "verified": verified,
            "attempt_number": attempt_number,
            "provider_refund_id": provider_id or refund.provider_refund_id,
            "identifiers_queried": {
                "provider_payment_id": attempt.provider_payment_id,
                "idem_key": refund.idem_key,
            },
            "amount": refund.amount,
            "refund_status_before": str(before),
            "refund_status_after": str(after),
            "state_before": str(state),
            "state_after": str(new_state),
            "next_action": next_action,
            "code": str(code),
            "explanation": explanation,
        },
        correlation_id=correlation_id,
    )
    return RefundReconciliation(
        refund_id=refund.id,
        payment_attempt_id=attempt.id,
        refund_status_before=before,
        refund_status_after=after,
        attempt_state_before=state,
        attempt_state_after=new_state,
        next_action=next_action,
        code=code,
        explanation=explanation,
    )


def _case_already_open(
    session: Session, tenant: uuid.UUID, attempt_id: uuid.UUID, case_key: str
) -> bool:
    """Whether this attempt's stream already records a case for ``case_key``.

    Read under the attempt row lock, so two escalators for one key serialize here and
    the second sees the first's committed event.
    """
    stream = audit.read_stream(
        session, tenant=tenant, aggregate_type=AGGREGATE_TYPE, aggregate_id=attempt_id
    )
    return any(
        event.event_type == "human_review.opened" and event.payload.get("case_key") == case_key
        for event in stream
    )


def escalate_refund(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    reason_family: str,
    correlation_id: uuid.UUID,
    attempts: int | None = None,
) -> RefundEscalation:
    """Freeze the attempt in ``ESCALATED`` and open exactly one human-review case.

    Called by the reconciliation path once :data:`MAX_RECONCILIATION_ATTEMPTS` rounds have
    not resolved a ``REFUND_UNKNOWN``, on contradictory evidence, or when a
    ``REFUND_FAILED`` refund has exhausted its retries. The refund row becomes
    ``ESCALATED``; the attempt moves through declared edges (``REFUND_UNKNOWN`` via
    ``RECONCILING``) into the terminal ``ESCALATED``, which no automated edge leaves.

    Exactly-once (specification 6.4.3): the case key is
    :func:`human_review_case_key` over tenant, checkout, refund and ``reason_family``.
    Under the attempt row lock the attempt's stream is scanned for a ``human_review.opened``
    event with that key; one is appended only if none exists, and ``opened`` reports which.
    A second escalation of the same refund for the same family is therefore a no-op that
    returns the same key. An already ``ESCALATED`` attempt is left as it is.

    Raises :class:`RefundStateError` for a refund that is ``PENDING`` or ``PROCESSED``
    (there is nothing to review) or an attempt in a state with no path to ``ESCALATED``.
    """
    tenant = _bound_tenant(session, tenant_id)
    if not isinstance(correlation_id, uuid.UUID):
        raise RefundUsageError("correlation_id must be a UUID")
    attempt, refund = _locked_pair(session, tenant, refund_id)
    case_key = human_review_case_key(
        tenant_id=tenant,
        order_ref=attempt.checkout_id,
        subject_id=refund.id,
        reason_family=reason_family,
    )
    state = attempt.status
    new_state = state

    if refund.status is not RefundStatus.ESCALATED:
        if refund.status not in _ESCALATABLE_REFUNDS:
            raise RefundStateError(
                f"refund {refund_id} is {refund.status}; only an UNKNOWN, RECONCILING or "
                "FAILED refund is escalated",
                refund_id=refund_id,
            )
        _set_refund_status(session, tenant, refund.id, RefundStatus.ESCALATED)

    if state is not PaymentState.ESCALATED:
        if state not in _ESCALATABLE_STATES:
            raise RefundStateError(
                f"attempt {attempt.id} is {state}; no refund escalation edge leads from it",
                refund_id=refund_id,
            )
        route = _advance_attempt(session, tenant, attempt, PaymentState.ESCALATED)
        new_state = route[-1]
        _audit(
            session,
            tenant,
            attempt.id,
            "refund.escalated",
            actor_type=ActorType.WORKER,
            principal_id=None,
            payload={
                "refund_id": refund.id,
                "case_key": case_key,
                "reason_family": reason_family,
                "attempts": attempts,
                "refund_status_before": str(refund.status),
                "state_before": str(state),
                "state_after": str(new_state),
                "route": [str(step) for step in route],
            },
            correlation_id=correlation_id,
        )

    opened = not _case_already_open(session, tenant, attempt.id, case_key)
    if opened:
        _audit(
            session,
            tenant,
            attempt.id,
            "human_review.opened",
            actor_type=ActorType.WORKER,
            principal_id=None,
            payload={
                "case_key": case_key,
                "reason_family": reason_family,
                "refund_id": refund.id,
                "payment_attempt_id": attempt.id,
                "checkout_id": attempt.checkout_id,
                "checkout_version": attempt.checkout_version,
                "provider_payment_id": attempt.provider_payment_id,
                "provider_refund_id": refund.provider_refund_id,
                "idem_key": refund.idem_key,
                "monetary_exposure": refund.amount,
                "attempts": attempts,
                "max_attempts": MAX_RECONCILIATION_ATTEMPTS,
                "code": str(RecoveryCode.HUMAN_REVIEW_REQUIRED),
            },
            correlation_id=correlation_id,
        )
    return RefundEscalation(
        refund_id=refund.id,
        payment_attempt_id=attempt.id,
        case_key=case_key,
        opened=opened,
        attempt_state_before=state,
        attempt_state_after=new_state,
    )


# ------------------------------------------------------------------ provider-originated


def record_provider_originated_refund(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    provider_refund_id: str,
    amount: Money,
    correlation_id: uuid.UUID,
) -> RefundTransition:
    """Record a refund the provider made without this platform asking (11.3).

    A dashboard refund arrives as a webhook naming a refund id no local row carries. It
    is written as a ``PROCESSED`` row flagged ``provider_originated`` so the ledger
    matches the provider, and the attempt moves to ``REFUNDED`` or ``PARTIALLY_REFUNDED``
    when a declared path exists from its current state (an attempt in reconciliation is
    left to reconciliation).

    Never double counts:

    * a row already carrying ``provider_refund_id`` means the webhook describes a refund
      this platform made or already recorded -- no-op, ``DUPLICATE_OPERATION``;
    * a local ``PENDING``/``UNKNOWN``/``RECONCILING`` refund of the same amount with no
      provider id yet is very likely this same refund seen from the other side -- no-op,
      ``RECONCILIATION_IN_PROGRESS``; the caller resolves that row with
      :func:`record_refund_result` or :func:`reconcile_refund` and this id.

    Raises :class:`RefundLedgerError` when the amount exceeds what the capture can still
    refund or is in another currency: the provider is authoritative, but a ledger that
    cannot absorb its figure needs a person, not a silent overwrite.
    """
    tenant = _bound_tenant(session, tenant_id)
    provider_id = _check_provider_id(provider_refund_id, required=True)
    assert provider_id is not None  # noqa: S101 - narrowed by required=True
    if not isinstance(amount, Money) or amount.minor <= 0:
        raise RefundUsageError("amount must be a positive Money")
    if not isinstance(correlation_id, uuid.UUID):
        raise RefundUsageError("correlation_id must be a UUID")

    attempt = _lock_attempt(session, tenant, payment_attempt_id)
    if attempt is None:
        raise RefundNotFoundError(f"no payment attempt {payment_attempt_id} for this tenant")
    state = attempt.status

    known = session.execute(
        _SELECT_BY_PROVIDER_ID, {"t": tenant, "a": attempt.id, "pid": provider_id}
    ).one_or_none()
    if known is not None:
        row = _refund_view(known)
        return RefundTransition(
            refund_id=row.id,
            payment_attempt_id=attempt.id,
            refund_status_before=row.status,
            refund_status_after=row.status,
            attempt_state_before=state,
            attempt_state_after=state,
            changed=False,
            code=RecoveryCode.DUPLICATE_OPERATION,
            explanation="provider_refund_already_recorded",
        )
    if amount.currency != attempt.amount.currency:
        raise RefundLedgerError(
            f"provider refund {provider_id} is in {amount.currency}, capture is in "
            f"{attempt.amount.currency}"
        )
    unresolved = session.execute(
        _COUNT_UNRESOLVED_LOCAL, {"t": tenant, "a": attempt.id, "amt": amount.minor}
    ).scalar_one()
    if int(unresolved) > 0:
        # Cannot name the row without guessing, so no row is named: the caller matches
        # the local refund to this id through the reconciliation path.
        return RefundTransition(
            refund_id=uuid.UUID(int=0),
            payment_attempt_id=attempt.id,
            refund_status_before=RefundStatus.UNKNOWN,
            refund_status_after=RefundStatus.UNKNOWN,
            attempt_state_before=state,
            attempt_state_after=state,
            changed=False,
            code=RecoveryCode.RECONCILIATION_IN_PROGRESS,
            explanation="matches_unresolved_local_refund",
        )
    book = _ledger(session, tenant, attempt)
    if amount > book.remaining:
        raise RefundLedgerError(
            f"provider refund {provider_id} of {amount.minor} exceeds the {book.remaining.minor} "
            f"still refundable on attempt {attempt.id}"
        )

    refund_id = uuid7()
    session.execute(
        _INSERT_REFUND,
        {
            "id": refund_id,
            "t": tenant,
            "a": attempt.id,
            "o": _order_of(session, tenant, attempt.id),
            "c": attempt.checkout_id,
            "status": RefundStatus.PROCESSED.value,
            "amt": amount.minor,
            "cur": amount.currency,
            # Namespaced by the provider's own id: stable, and can never collide with a
            # key this platform derived for a refund it admitted.
            "key": f"{_KEY_PREFIX}ext_{provider_id}",
            "pid": provider_id,
            "ext": True,
            "reason": PROVIDER_ORIGINATED_REASON,
        },
    )
    target = _settled_target(session, tenant, attempt)
    new_state = state
    route: tuple[PaymentState, ...] = ()
    if _edges_to(state, target) is not None:
        route = _advance_attempt(session, tenant, attempt, target)
        _settle_order(session, tenant, attempt, target)
        new_state = route[-1] if route else state

    _audit(
        session,
        tenant,
        attempt.id,
        "refund.provider_originated",
        actor_type=ActorType.WORKER,
        principal_id=None,
        payload={
            "refund_id": refund_id,
            "provider_refund_id": provider_id,
            "amount": amount,
            "captured": book.captured,
            "reserved_before": book.reserved,
            "state_before": str(state),
            "state_after": str(new_state),
            "route": [str(step) for step in route],
            "attempt_left_to_reconciliation": not route and state is not target,
        },
        correlation_id=correlation_id,
    )
    return RefundTransition(
        refund_id=refund_id,
        payment_attempt_id=attempt.id,
        refund_status_before=RefundStatus.PROCESSED,
        refund_status_after=RefundStatus.PROCESSED,
        attempt_state_before=state,
        attempt_state_after=new_state,
        changed=True,
        code=RecoveryCode.OK,
        explanation="provider_refund_recorded",
    )


# ---------------------------------------------------------------------------- reading


@dataclass(frozen=True, slots=True)
class RefundOffer:
    """What a capture can still refund once the sale's own terms have been consulted.

    Two facts that used to be one. ``ledger`` is arithmetic over captures and refunds;
    ``window`` is what the merchant wrote at sale time. A surface that showed only the
    first would offer a buyer money the kernel is about to refuse them, and a figure a
    buyer was shown and then denied is the one outcome a refund screen may not produce --
    the same rule the checkout screen obeys about the amount it asks consent for.
    """

    ledger: RefundLedger
    window: RefundWindow

    @property
    def refundable(self) -> Money:
        """What may actually be asked for now. Zero once the terms have closed.

        Zero rather than the ledger's remainder, because the remainder is true and
        misleading at the same time: the money exists, and none of it is available. A
        surface that renders the larger number and the refusal beside it has told a buyer
        two things they cannot reconcile.
        """
        if self.window.closed:
            return Money(0, self.ledger.captured.currency)
        return self.ledger.remaining

    @property
    def anything_remains(self) -> bool:
        return not self.refundable.is_zero


def refund_offer(
    session: Session, *, tenant_id: uuid.UUID, payment_attempt_id: uuid.UUID
) -> RefundOffer:
    """The ledger and the at-sale terms together, read as one admission would read them.

    The display counterpart of :func:`admit_refund`, and deliberately built from the same
    two reads in the same order, so that what a buyer is shown and what they are then
    granted cannot disagree for any reason but time passing between the two calls.
    """
    tenant = _bound_tenant(session, tenant_id)
    attempt = _lock_attempt(session, tenant, payment_attempt_id)
    if attempt is None:
        raise RefundNotFoundError(f"no payment attempt {payment_attempt_id} for this tenant")
    return RefundOffer(
        ledger=_ledger(session, tenant, attempt),
        window=refund_window_at_sale(session, attempt.checkout, now_ms=database_now_ms(session)),
    )


def refundable_now(
    session: Session, *, tenant_id: uuid.UUID, payment_attempt_id: uuid.UUID
) -> RefundLedger:
    """The capture ledger of one attempt, for callers that must show what is refundable.

    Takes the same locks as admission so the figure is consistent with any refund being
    admitted at the same moment; a caller that only wants a display value pays a short
    wait rather than reading a number that a concurrent admission is about to change.
    """
    tenant = _bound_tenant(session, tenant_id)
    attempt = _lock_attempt(session, tenant, payment_attempt_id)
    if attempt is None:
        raise RefundNotFoundError(f"no payment attempt {payment_attempt_id} for this tenant")
    return _ledger(session, tenant, attempt)


ledger = refundable_now
