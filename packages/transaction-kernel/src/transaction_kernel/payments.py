"""The payment-attempt lifecycle after admission, and the evidence that drives it.

Specification 10.5, 10.7, 10.8, 11.2 and 11.3; ADR 0003 D4, D5, D8. Admission creates a
payment attempt in ``CREATED`` and issues the grant; everything that happens to that row
afterwards -- the provider order being created, a webhook or a reconciliation fetch
reporting what the money did, a browser callback naming a payment, an escalation to a
human -- is written here and nowhere else. This is also the only writer of the three
financial-evidence tables ``provider_requests``, ``orders`` and ``reconciliation_runs``.

Three rules shape every function:

**The caller owns the transaction; the tenant comes from the connection.** Every function
refuses to run outside an open transaction and refuses a ``tenant_id`` that differs from
the one bound to it (``app.tenant_id``), so a worker cannot be talked into writing
another tenant's attempt by a payload it received. Nothing here commits or rolls back;
a failure raises and the caller's transaction must not commit.

**Locks are taken in the ADR D5 order.** An attempt is located with a plain SELECT, then
``checkout_versions`` is locked ``FOR UPDATE``, then (where a release may follow) the
reservation, then ``payment_attempts FOR UPDATE``. Cancel, evidence application and
escalation therefore queue on the version row instead of deadlocking on each other, and
two detectors of the same problem see each other's work.

**State moves only through :mod:`transaction_kernel.states`.** A deliberate step uses
``assert_transition``; an inbound provider fact uses ``monotonic_apply``, which is what
makes a late ``authorized`` after a capture a no-op instead of a rewind, and a duplicated
webhook harmless. The ``UPDATE`` that follows is guarded on the state that was read under
the lock, so a transition the table permits but the row no longer matches is refused
rather than applied blind.

Every state change is audited in the same transaction on the checkout's stream, so the
Money Action Proof Chain for a checkout is one hash chain from admission to order.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from commerce_domain import ActorType, DomainError, Money, RecoveryCode, canonical_hash, uuid7
from platform_db import require_tenant
from sqlalchemy import Row, TextClause, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, reservations
from .contracts import Operation
from .evidence import EvidenceSource, ProviderEvidence, may_fulfil
from .states import (
    CheckoutState,
    PaymentState,
    assert_transition,
    can_transition,
    monotonic_apply,
)

__all__ = [
    "RECONCILIATION_ATTEMPT_BOUND",
    "AttemptNotFoundError",
    "AttemptTransition",
    "AttemptView",
    "BrowserCallbackRecorded",
    "Escalation",
    "EvidenceApplied",
    "InboxRowNotFoundError",
    "PaymentStateConflictError",
    "PaymentsError",
    "PaymentsTenantError",
    "PaymentsUsageError",
    "ProviderOrderConflictError",
    "ProviderOrderOutcome",
    "apply_provider_evidence",
    "begin_reconciling",
    "escalate",
    "find_attempt_by_provider_order",
    "find_attempt_by_provider_payment",
    "read_attempt",
    "record_browser_callback",
    "record_create_order_result",
    "record_provider_request",
    "record_recovered_order",
    "record_reconciliation_run",
    "record_webhook_applied",
]

#: ADR D13: reconciliation is bounded to this many attempts before ``ESCALATED``. Exported
#: so the worker and the kernel count the same way; :func:`record_reconciliation_run`
#: refuses a higher ``attempt_number`` because a seventh run means the bound was not applied.
RECONCILIATION_ATTEMPT_BOUND: Final = 6

#: The audit stream every payment event is written to. Admission writes to the same
#: stream, so one chain covers admission -> provider request -> evidence -> order.
_AGGREGATE: Final = "checkout"

_UNIQUE_VIOLATION: Final = "23505"
_PROVIDER_ORDER_INDEX: Final = "uq_payment_attempts_provider_order"
_RECONCILIATION_RUN_UNIQUE: Final = "uq_reconciliation_runs_attempt_reason_number"

_IDENTIFIER: Final = re.compile(r"^\S{1,64}$")
_HEX_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_REASON: Final = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_HTTP_METHODS: Final[frozenset[str]] = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

#: Checkout states in which a capture can never be fulfilled. A capture arriving here is a
#: stale capture (specification 10.8): it is recorded against the attempt so that it is
#: refunded, and no order row is ever written for it. ``INVALIDATED_AWAITING_PAYMENT_RESULT``
#: is the designed case; the three terminal states are included because a checkout that
#: is already invalidated, cancelled or expired is owed a refund just the same, and treating
#: its capture as a sale would fulfil a version the buyer never approved.
_NEVER_FULFIL: Final[frozenset[CheckoutState]] = frozenset(
    {
        CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT,
        CheckoutState.INVALIDATED,
        CheckoutState.CANCELLED,
        CheckoutState.EXPIRED,
    }
)

#: The legal route from each live state to ``ESCALATED``. The transition table permits
#: ``ESCALATED`` only from ``RECONCILING`` and ``REFUND_FAILED``, which is deliberate: an
#: escalation means the platform no longer knows what the money did, and the route says
#: so -- a submitted attempt becomes unknown, unknown enters reconciliation, and
#: reconciliation gives up. States absent from this table (money moved: ``CAPTURED``,
#: ``STALE_CAPTURE`` and the refund postures; and the terminal states) cannot be frozen by
#: an escalation; a case is opened for them without a state change.
_ESCALATION_PATH: Final[Mapping[PaymentState, tuple[PaymentState, ...]]] = {
    PaymentState.CREATED: (PaymentState.UNKNOWN, PaymentState.RECONCILING, PaymentState.ESCALATED),
    PaymentState.SUBMITTED: (
        PaymentState.UNKNOWN,
        PaymentState.RECONCILING,
        PaymentState.ESCALATED,
    ),
    PaymentState.AUTHORIZED: (
        PaymentState.UNKNOWN,
        PaymentState.RECONCILING,
        PaymentState.ESCALATED,
    ),
    PaymentState.UNKNOWN: (PaymentState.RECONCILING, PaymentState.ESCALATED),
    PaymentState.RECONCILING: (PaymentState.ESCALATED,),
    PaymentState.REFUND_UNKNOWN: (PaymentState.RECONCILING, PaymentState.ESCALATED),
    PaymentState.REFUND_FAILED: (PaymentState.ESCALATED,),
}


# ----------------------------------------------------------------------------- errors


class PaymentsError(DomainError):
    """Base for this module. Carries a ``RecoveryCode`` so a caller can map it (D15)."""

    code: RecoveryCode = RecoveryCode.HUMAN_REVIEW_REQUIRED

    def __init__(self, message: str, *, code: RecoveryCode | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class PaymentsUsageError(PaymentsError):
    """The caller broke the contract: no transaction, a malformed argument, a wrong step.

    A programming error rather than a business outcome; the transaction must abort.
    """

    code = RecoveryCode.POLICY_EXCEPTION


class PaymentsTenantError(PaymentsError):
    """``tenant_id`` names a tenant other than the one bound to this transaction."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class AttemptNotFoundError(PaymentsError):
    """No attempt with that id is visible to this tenant. Cross-tenant ids read as absent."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT

    def __init__(self, payment_attempt_id: uuid.UUID) -> None:
        super().__init__(f"payment attempt {payment_attempt_id} is not visible to this tenant")
        self.payment_attempt_id = payment_attempt_id


class InboxRowNotFoundError(PaymentsError):
    """No webhook inbox row with that id is visible to this tenant."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class PaymentStateConflictError(PaymentsError):
    """The attempt is not in the state this deliberate step requires.

    Raised when a worker asks for ``CREATED -> SUBMITTED`` on an attempt that a concurrent
    actor already moved elsewhere. Carries both states so the caller can read the row and
    decide, rather than retrying a step the lifecycle no longer permits.
    """

    code = RecoveryCode.CONCURRENT_OPERATION

    def __init__(self, current: PaymentState, target: PaymentState, *, step: str) -> None:
        super().__init__(f"{step}: attempt is {current.value}, cannot move to {target.value}")
        self.current = current
        self.target = target


class ProviderOrderConflictError(PaymentsError):
    """The provider order id is already bound to another attempt of this tenant.

    One Razorpay order belongs to exactly one attempt (``uq_payment_attempts_provider_order``).
    Seeing it twice means two attempts claim one order, which is exactly the situation a
    human must look at before any money moves on either.
    """

    code = RecoveryCode.HUMAN_REVIEW_REQUIRED

    def __init__(self, provider_order_id: str) -> None:
        super().__init__(
            f"provider order {provider_order_id!r} is already bound to another attempt"
        )
        self.provider_order_id = provider_order_id


# ------------------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class AttemptView:
    """One ``payment_attempts`` row, read under the caller's tenant."""

    attempt_id: uuid.UUID
    tenant_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    status: PaymentState
    amount: Money
    receipt: str
    provider_order_id: str | None
    provider_payment_id: str | None


@dataclass(frozen=True, slots=True)
class _VersionRow:
    """The locked ``checkout_versions`` row an attempt is bound to."""

    version_id: uuid.UUID
    merchant_id: uuid.UUID
    status: CheckoutState
    policy_receipt_id: uuid.UUID | None
    policy_receipt_hash: str | None


@dataclass(frozen=True, slots=True)
class AttemptTransition:
    """What a deliberate lifecycle step did. ``state_before == state_after`` is a no-op."""

    state_before: PaymentState
    state_after: PaymentState
    checkout_state_after: CheckoutState

    @property
    def changed(self) -> bool:
        return self.state_before is not self.state_after


@dataclass(frozen=True, slots=True)
class ProviderOrderOutcome:
    """What the worker's create-order call established.

    ``ok`` requires the provider's order id; ``failed`` means the provider confirmed no
    order exists (a 4xx on a mutation), ``unknown`` means the response was lost and the
    order may exist (specification 10.6: lookup by receipt before any second create).
    ``code`` and ``reason`` are copied into the audit row verbatim.
    """

    kind: Literal["ok", "failed", "unknown"]
    provider_order_id: str | None
    code: RecoveryCode
    reason: str

    def __post_init__(self) -> None:
        if self.kind not in ("ok", "failed", "unknown"):
            raise PaymentsUsageError(f"kind must be ok, failed or unknown, got {self.kind!r}")
        if self.kind == "ok":
            if self.provider_order_id is None:
                raise PaymentsUsageError("an ok outcome must carry the provider order id")
            _require_identifier(self.provider_order_id, "provider_order_id")
        elif self.provider_order_id is not None:
            raise PaymentsUsageError(f"a {self.kind} outcome cannot carry a provider order id")
        if not isinstance(self.code, RecoveryCode):
            raise PaymentsUsageError(f"code must be a RecoveryCode, got {self.code!r}")
        _require_reason(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class EvidenceApplied:
    """What applying one evidence record did.

    ``changed`` is false for a duplicate or a late, weaker report; ``order_id`` is the
    confirmed order whenever the attempt is ``CAPTURED`` on sufficient evidence, including
    on a duplicate; ``stale_capture`` says a capture landed on a checkout that can never be
    fulfilled; ``case_key`` is set when mismatched evidence opened (or found) a human
    review case; ``refund_reported`` says the provider already returned some of the money
    and the refund ledger must be reconciled.
    """

    state_before: PaymentState
    state_after: PaymentState
    changed: bool
    order_id: uuid.UUID | None
    stale_capture: bool
    reason: str
    checkout_state_after: CheckoutState
    case_key: str | None = None
    refund_reported: bool = False


@dataclass(frozen=True, slots=True)
class BrowserCallbackRecorded:
    """The verdict on one client-return. Never a payment state change (ADR D8)."""

    accepted: bool
    code: RecoveryCode
    reason: str
    state: PaymentState
    provider_payment_id: str | None


@dataclass(frozen=True, slots=True)
class Escalation:
    """One human review case. ``opened`` is true for exactly one caller per case key."""

    case_key: str
    state_before: PaymentState
    state_after: PaymentState
    opened: bool


# ------------------------------------------------------------------------ guards


def _require_transaction(session: Session) -> None:
    if not session.in_transaction():
        raise PaymentsUsageError(
            "payments functions require an open transaction: the row lock, the state change "
            "and the audit row must commit together or not at all"
        )


def _check_tenant(session: Session, tenant_id: uuid.UUID) -> uuid.UUID:
    """The tenant argument must equal the connection's tenant; the GUC is the authority."""
    if not isinstance(tenant_id, uuid.UUID):
        raise PaymentsUsageError(f"tenant_id must be a UUID, got {type(tenant_id).__name__}")
    bound: uuid.UUID = require_tenant(session)
    if bound != tenant_id:
        raise PaymentsTenantError(
            f"call names tenant {tenant_id} but this transaction is bound to {bound}"
        )
    return bound


def _bound_tenant(session: Session, tenant_id: uuid.UUID, correlation_id: uuid.UUID) -> uuid.UUID:
    """Every entry point's preconditions: an open transaction and the bound tenant."""
    _require_transaction(session)
    if not isinstance(correlation_id, uuid.UUID):
        raise PaymentsUsageError("correlation_id must be a UUID; it is the dispute thread")
    return _check_tenant(session, tenant_id)


def _require_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise PaymentsUsageError(
            f"{name} must be a non-empty provider identifier without whitespace (<= 64), "
            f"got {value!r}"
        )
    return value


def _require_reason(value: object, name: str) -> str:
    if not isinstance(value, str) or not _REASON.match(value):
        raise PaymentsUsageError(
            f"{name} must be a short machine-readable token [a-z0-9_.:-], got {value!r}"
        )
    return value


def _require_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.match(value):
        raise PaymentsUsageError(f"{name} must be lowercase hex SHA-256, got {value!r}")
    return value


_OPERATION_NAME: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


def _operation_name(operation: Operation | str) -> str:
    """A mutation from the closed enum, or an upper-case token naming a read."""
    if isinstance(operation, Operation):
        return operation.value
    if not isinstance(operation, str) or not _OPERATION_NAME.match(operation):
        raise PaymentsUsageError(
            f"operation must be an Operation or an UPPER_SNAKE token (<= 32), got {operation!r}"
        )
    return operation


def _require_uuid(value: object, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise PaymentsUsageError(f"{name} must be a UUID, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------- SQL


def _stmt(*parts: str) -> TextClause:
    return text(" ".join(parts))


_ATTEMPT_COLUMNS: Final = (
    "SELECT id, tenant_id, checkout_id, checkout_version, status, amount_minor, currency, "
    "receipt, provider_order_id, provider_payment_id FROM payment_attempts"
)

_LOCATE_ATTEMPT: Final = _stmt(_ATTEMPT_COLUMNS, "WHERE tenant_id = :t AND id = :a")
_LOCK_ATTEMPT: Final = _stmt(_ATTEMPT_COLUMNS, "WHERE tenant_id = :t AND id = :a FOR UPDATE")
_BY_PROVIDER_ORDER: Final = _stmt(
    _ATTEMPT_COLUMNS, "WHERE tenant_id = :t AND provider_order_id = :o"
)
_BY_PROVIDER_PAYMENT: Final = _stmt(
    _ATTEMPT_COLUMNS,
    "WHERE tenant_id = :t AND provider_payment_id = :p ORDER BY created_at DESC LIMIT 2",
)

_LOCK_VERSION: Final = _stmt(
    "SELECT id, merchant_id, status, policy_receipt_id, policy_receipt_hash",
    "FROM checkout_versions WHERE tenant_id = :t AND checkout_id = :c AND version = :v",
    "FOR UPDATE",
)

#: Guarded on the state read under the lock. ``COALESCE`` keeps an identifier that is
#: already recorded; a caller wanting to overwrite one has no path here, by design.
_MOVE_ATTEMPT: Final = _stmt(
    "UPDATE payment_attempts SET status = :after, updated_at = now(),",
    "provider_order_id = COALESCE(CAST(:po AS varchar), provider_order_id),",
    "provider_payment_id = COALESCE(CAST(:pp AS varchar), provider_payment_id)",
    "WHERE tenant_id = :t AND id = :a AND status = :before RETURNING id",
)

_MOVE_VERSION: Final = _stmt(
    "UPDATE checkout_versions SET status = :after",
    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v AND status = :before",
    "RETURNING id",
)

#: The API-owned head mirrors the current version's state for cheap reads. Best effort:
#: kernel-only callers (and kernel tests) may have no head row, and that is not an error.
_MOVE_HEAD: Final = _stmt(
    "UPDATE checkouts SET status = :after, updated_at = now()",
    "WHERE tenant_id = :t AND id = :c AND current_version = :v",
)

_INSERT_ORDER: Final = _stmt(
    "INSERT INTO orders (id, tenant_id, merchant_id, checkout_id, checkout_version,",
    "payment_attempt_id, policy_receipt_id, policy_receipt_hash, total_minor, currency,",
    "status, capture_evidence)",
    "VALUES (:id, :t, :m, :c, :v, :a, :rid, :rh, :total, :cur, 'CONFIRMED',",
    "CAST(:evidence AS jsonb))",
    "ON CONFLICT (tenant_id, payment_attempt_id) DO NOTHING RETURNING id",
)

_FIND_ORDER: Final = _stmt("SELECT id FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a")

_INSERT_PROVIDER_REQUEST: Final = _stmt(
    "INSERT INTO provider_requests (id, tenant_id, payment_attempt_id, grant_id, refund_id,",
    "operation, method, url, body_hash, header_names, http_status, provider_id,",
    "outcome_code, provider_error_code, response_digest, transport_error)",
    "VALUES (:id, :t, :a, :g, :r, :op, :method, :url, :body_hash,",
    "CAST(:header_names AS jsonb), :http_status, :provider_id, :outcome_code,",
    ":provider_error_code, :response_digest, :transport_error)",
)

_GRANT_FOR_REQUEST: Final = _stmt(
    "SELECT payment_attempt_id, status FROM execution_grants WHERE tenant_id = :t AND id = :g"
)
_REQUESTS_FOR_GRANT: Final = _stmt(
    "SELECT count(*) FROM provider_requests WHERE tenant_id = :t AND grant_id = :g"
)

_INSERT_RECONCILIATION_RUN: Final = _stmt(
    "INSERT INTO reconciliation_runs (id, tenant_id, payment_attempt_id, refund_id,",
    "attempt_number, reason, identifiers_queried, raw_evidence_digest, decision,",
    "resulting_transition, next_scheduled_attempt, correlation_id)",
    "VALUES (:id, :t, :a, :r, :n, :reason, CAST(:identifiers AS jsonb), :digest, :decision,",
    ":transition,",
    "CASE WHEN CAST(:delay AS integer) IS NULL THEN NULL",
    "ELSE now() + make_interval(secs => CAST(:delay AS integer)) END,",
    ":corr)",
)

_UPDATE_INBOX: Final = _stmt(
    "UPDATE webhook_inbox SET applied_at = now(), apply_status = :status, apply_reason = :reason,",
    "state_before = :before, state_after = :after, changed = :changed,",
    "outbox_command_id = :cmd WHERE tenant_id = :t AND id = :i RETURNING id",
)

_CASE_EXISTS: Final = _stmt(
    "SELECT 1 FROM audit_events WHERE tenant_id = :t AND aggregate_type = :agg",
    "AND aggregate_id = :c AND event_type = 'human_review.opened'",
    "AND payload ->> 'case_key' = :k LIMIT 1",
)


# ------------------------------------------------------------------- row helpers


def _view(row: Row[Any]) -> AttemptView:
    return AttemptView(
        attempt_id=row.id,
        tenant_id=row.tenant_id,
        checkout_id=row.checkout_id,
        checkout_version=int(row.checkout_version),
        status=PaymentState(row.status),
        amount=Money(int(row.amount_minor), row.currency),
        receipt=row.receipt,
        provider_order_id=row.provider_order_id,
        provider_payment_id=row.provider_payment_id,
    )


def _read_attempt_unlocked(
    session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID
) -> AttemptView:
    """Plain SELECT: learn which version to lock without holding the attempt row yet."""
    row = session.execute(_LOCATE_ATTEMPT, {"t": tenant_id, "a": attempt_id}).one_or_none()
    if row is None:
        raise AttemptNotFoundError(attempt_id)
    return _view(row)


def _lock_version(session: Session, tenant_id: uuid.UUID, located: AttemptView) -> _VersionRow:
    row = session.execute(
        _LOCK_VERSION,
        {"t": tenant_id, "c": located.checkout_id, "v": located.checkout_version},
    ).one_or_none()
    if row is None:
        # The attempt exists but its version does not: the FK-less checkout_id is
        # dangling. Not a business outcome; refuse loudly.
        raise PaymentsUsageError(
            f"attempt {located.attempt_id} is bound to checkout {located.checkout_id} "
            f"version {located.checkout_version}, which does not exist for this tenant"
        )
    return _VersionRow(
        version_id=row.id,
        merchant_id=row.merchant_id,
        status=CheckoutState(row.status),
        policy_receipt_id=row.policy_receipt_id,
        policy_receipt_hash=row.policy_receipt_hash,
    )


def _lock_attempt(session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID) -> AttemptView:
    """Re-read under ``FOR UPDATE``: the state judged is the committed one, not the located one."""
    row = session.execute(_LOCK_ATTEMPT, {"t": tenant_id, "a": attempt_id}).one_or_none()
    if row is None:  # pragma: no cover - the row was visible a statement ago
        raise AttemptNotFoundError(attempt_id)
    return _view(row)


def _lock_version_and_attempt(
    session: Session,
    tenant_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    lock_reservation: bool = False,
) -> tuple[AttemptView, _VersionRow]:
    """The D5 sequence: locate, lock the version, (the reservation), lock the attempt.

    ``lock_reservation`` is set by callers that may go on to release the hold, so the
    reservation row is taken between the version and the attempt -- the documented order
    -- rather than after the attempt lock inside :func:`reservations.release`.
    """
    located = _read_attempt_unlocked(session, tenant_id, attempt_id)
    version = _lock_version(session, tenant_id, located)
    if lock_reservation:
        reservations.check_validity(
            session,
            checkout_id=located.checkout_id,
            checkout_version=located.checkout_version,
            lock=True,
        )
    return _lock_attempt(session, tenant_id, attempt_id), version


def _move_attempt(
    session: Session,
    attempt: AttemptView,
    after: PaymentState,
    *,
    provider_order_id: str | None = None,
    provider_payment_id: str | None = None,
) -> None:
    """Guarded UPDATE. The row must still hold the state judged under the lock."""
    try:
        with session.begin_nested():
            row = session.execute(
                _MOVE_ATTEMPT,
                {
                    "after": after.value,
                    "po": provider_order_id,
                    "pp": provider_payment_id,
                    "t": attempt.tenant_id,
                    "a": attempt.attempt_id,
                    "before": attempt.status.value,
                },
            ).one_or_none()
    except IntegrityError as exc:
        if _is_violation_of(exc, _PROVIDER_ORDER_INDEX) and provider_order_id is not None:
            raise ProviderOrderConflictError(provider_order_id) from exc
        raise
    if row is None:  # pragma: no cover - requires losing a row lock we hold
        raise PaymentStateConflictError(attempt.status, after, step="guarded_update")


def _move_checkout(
    session: Session,
    tenant_id: uuid.UUID,
    attempt: AttemptView,
    version: _VersionRow,
    after: CheckoutState,
) -> CheckoutState:
    """A deliberate checkout step: asserted against the table, guarded on the row."""
    assert_transition(version.status, after)
    row = session.execute(
        _MOVE_VERSION,
        {
            "after": after.value,
            "t": tenant_id,
            "c": attempt.checkout_id,
            "v": attempt.checkout_version,
            "before": version.status.value,
        },
    ).one_or_none()
    if row is None:  # pragma: no cover - requires losing a row lock we hold
        raise PaymentsUsageError("checkout version moved underneath a lock this transaction holds")
    session.execute(
        _MOVE_HEAD,
        {
            "after": after.value,
            "t": tenant_id,
            "c": attempt.checkout_id,
            "v": attempt.checkout_version,
        },
    )
    return after


def _move_checkout_if_legal(
    session: Session,
    tenant_id: uuid.UUID,
    attempt: AttemptView,
    version: _VersionRow,
    after: CheckoutState,
) -> CheckoutState:
    """Move when the table allows it from the current state; otherwise keep the state.

    Used where the payment fact must be recorded whatever the checkout is doing: a
    provider-confirmed result is not less true because the head is in an unexpected
    state, and refusing to record it would lose evidence. The audit payload names the
    state that was kept.
    """
    if version.status is after or not can_transition(version.status, after):
        return version.status
    return _move_checkout(session, tenant_id, attempt, version, after)


def _is_violation_of(exc: IntegrityError, constraint: str) -> bool:
    orig = exc.orig
    if getattr(orig, "sqlstate", None) != _UNIQUE_VIOLATION:
        return False
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint


def _audit(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    attempt: AttemptView,
    event_type: str,
    actor_type: ActorType,
    payload: Mapping[str, Any],
    correlation_id: uuid.UUID,
    principal_id: str | None = None,
) -> None:
    body = {
        "payment_attempt_id": str(attempt.attempt_id),
        "checkout_version": attempt.checkout_version,
        **payload,
    }
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=_AGGREGATE,
        aggregate_id=attempt.checkout_id,
        event_type=event_type,
        actor_type=actor_type,
        principal_id=principal_id,
        payload=body,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------- reads


def read_attempt(
    session: Session, *, tenant_id: uuid.UUID, payment_attempt_id: uuid.UUID
) -> AttemptView | None:
    """The attempt as this tenant sees it, or ``None``. Plain read, no lock."""
    _require_transaction(session)
    _check_tenant(session, tenant_id)
    row = session.execute(
        _LOCATE_ATTEMPT,
        {"t": tenant_id, "a": _require_uuid(payment_attempt_id, "payment_attempt_id")},
    ).one_or_none()
    return None if row is None else _view(row)


def find_attempt_by_provider_order(
    session: Session, *, tenant_id: uuid.UUID, provider_order_id: str
) -> AttemptView | None:
    """The one attempt bound to a provider order id, or ``None``.

    Uniqueness is the partial index ``uq_payment_attempts_provider_order``; this is the
    webhook and client-return path from an ``order_…`` id back to the row it concerns,
    and it is tenant-scoped so another tenant's order id reads as absent (ADR D7).
    """
    _require_transaction(session)
    _check_tenant(session, tenant_id)
    row = session.execute(
        _BY_PROVIDER_ORDER,
        {"t": tenant_id, "o": _require_identifier(provider_order_id, "provider_order_id")},
    ).one_or_none()
    return None if row is None else _view(row)


def find_attempt_by_provider_payment(
    session: Session, *, tenant_id: uuid.UUID, provider_payment_id: str
) -> AttemptView | None:
    """The attempt that recorded a provider payment id, or ``None``.

    The index is not unique, because nothing in the schema forbids two attempts naming one
    payment; if that ever happens it is a data fault a person must look at, so two rows
    raise ``PaymentsError(HUMAN_REVIEW_REQUIRED)`` rather than returning the newer one.
    """
    _require_transaction(session)
    _check_tenant(session, tenant_id)
    rows = session.execute(
        _BY_PROVIDER_PAYMENT,
        {"t": tenant_id, "p": _require_identifier(provider_payment_id, "provider_payment_id")},
    ).all()
    if not rows:
        return None
    if len(rows) > 1:
        raise PaymentsError(
            f"provider payment {provider_payment_id!r} is recorded on more than one attempt",
            code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
        )
    return _view(rows[0])


# ------------------------------------------------------------- provider requests


def record_provider_request(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    grant_id: uuid.UUID | None,
    refund_id: uuid.UUID | None,
    operation: Operation | str,
    method: str,
    url: str,
    body_hash: str,
    header_names: Sequence[str],
    http_status: int | None,
    provider_id: str | None,
    outcome_code: RecoveryCode | str,
    provider_error_code: str | None,
    response_digest: str | None,
    transport_error: str | None,
    correlation_id: uuid.UUID,
) -> uuid.UUID:
    """Record one HTTP call to the provider: proof-chain link 8.

    Guarantees:

    * nothing secret is stored. The URL is refused if it carries a query string or
      userinfo, the body is a digest, only header *names* are kept, and the response is a
      digest. A reviewer can prove which bytes went out and came back without this table
      ever being able to leak a key;
    * a mutation (``operation`` is one of :class:`Operation`) must name the Execution Grant
      it consumed, that grant must belong to this attempt and be ``CONSUMED``, and no
      request may already be recorded against it. "Every provider mutation consumes
      exactly one grant" is thereby checkable after the fact, which is what the proof
      verifier does; a read (``PAYMENT_FETCH``, ``ORDER_LOOKUP``, ...) carries no grant.

    Audits ``provider.request_recorded`` on the checkout stream. Returns the row id.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    op = _operation_name(operation)
    if method.upper() not in _HTTP_METHODS:
        raise PaymentsUsageError(f"method must be an HTTP method, got {method!r}")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise PaymentsUsageError("url must be an https URL")
    if "?" in url or "#" in url or "@" in url:
        raise PaymentsUsageError(
            "url must carry no query string, fragment or userinfo; a provider request row "
            "records the resource, never a credential or a parameter"
        )
    _require_digest(body_hash, "body_hash")
    if response_digest is not None:
        _require_digest(response_digest, "response_digest")
    names = [str(n) for n in header_names]
    if any(not n or any(ch.isspace() for ch in n) or ":" in n for n in names):
        raise PaymentsUsageError("header_names must be bare header names, never 'Name: value'")
    if http_status is not None and not 100 <= int(http_status) <= 599:
        raise PaymentsUsageError(f"http_status {http_status} is not an HTTP status")
    code = outcome_code.value if isinstance(outcome_code, RecoveryCode) else str(outcome_code)
    if len(code) > 48:
        raise PaymentsUsageError("outcome_code exceeds 48 characters")
    if provider_id is not None:
        _require_identifier(provider_id, "provider_id")
    if transport_error is not None and len(transport_error) > 64:
        raise PaymentsUsageError("transport_error exceeds 64 characters; store a class name")
    if provider_error_code is not None and len(provider_error_code) > 128:
        raise PaymentsUsageError("provider_error_code exceeds 128 characters")

    attempt = _read_attempt_unlocked(session, tenant_id, attempt_id)

    is_mutation = op in {o.value for o in Operation}
    if is_mutation:
        if grant_id is None:
            raise PaymentsUsageError(
                f"a {op} request must name the Execution Grant it consumed; a provider "
                "mutation without a grant is the thing this platform exists to prevent"
            )
        grant = session.execute(
            _GRANT_FOR_REQUEST, {"t": tenant_id, "g": _require_uuid(grant_id, "grant_id")}
        ).one_or_none()
        if grant is None or grant.payment_attempt_id != attempt_id:
            raise PaymentsError(
                f"grant {grant_id} is not a grant of attempt {attempt_id}",
                code=RecoveryCode.AUTHORITY_INSUFFICIENT,
            )
        if grant.status != "CONSUMED":
            raise PaymentsError(
                f"grant {grant_id} is {grant.status}, not CONSUMED; a request is recorded "
                "only for a grant that was consumed before the provider was called",
                code=RecoveryCode.AUTHORITY_INSUFFICIENT,
            )
        already = session.execute(_REQUESTS_FOR_GRANT, {"t": tenant_id, "g": grant_id}).scalar()
        if already:
            raise PaymentsError(
                f"grant {grant_id} already has a provider request recorded; one grant, one "
                "mutation",
                code=RecoveryCode.DUPLICATE_OPERATION,
            )
    elif grant_id is not None:
        raise PaymentsUsageError(f"a {op} read does not consume a grant; grant_id must be None")

    request_id = uuid7()
    session.execute(
        _INSERT_PROVIDER_REQUEST,
        {
            "id": request_id,
            "t": tenant_id,
            "a": attempt_id,
            "g": grant_id,
            "r": refund_id,
            "op": op,
            "method": method.upper(),
            "url": url,
            "body_hash": body_hash,
            "header_names": json.dumps(names),
            "http_status": http_status,
            "provider_id": provider_id,
            "outcome_code": code,
            "provider_error_code": provider_error_code,
            "response_digest": response_digest,
            "transport_error": transport_error,
        },
    )
    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="provider.request_recorded",
        actor_type=ActorType.WORKER,
        payload={
            "provider_request_id": str(request_id),
            "grant_id": None if grant_id is None else str(grant_id),
            "refund_id": None if refund_id is None else str(refund_id),
            "operation": op,
            "method": method.upper(),
            "url": url,
            "body_hash": body_hash,
            "http_status": http_status,
            "provider_id": provider_id,
            "outcome_code": code,
            "provider_error_code": provider_error_code,
            "response_digest": response_digest,
            "transport_error": transport_error,
        },
        correlation_id=correlation_id,
    )
    return request_id


# ------------------------------------------------------------- create-order result


def record_create_order_result(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    outcome: ProviderOrderOutcome,
    correlation_id: uuid.UUID,
) -> AttemptTransition:
    """Record what the create-order call established (ADR D4, specification 10.6).

    Transitions used, quoted from ``states``:

    * ``ok``: ``PaymentState.CREATED -> SUBMITTED`` and
      ``CheckoutState.EXECUTION_PENDING -> AWAITING_PAYMENT``; the provider order id is
      stored, and a second attempt claiming the same order id is refused with
      :class:`ProviderOrderConflictError`;
    * ``failed``: ``CREATED -> FAILED`` and ``EXECUTION_PENDING -> PAYMENT_FAILED``; the
      reservation is released with cause ``PAYMENT_FAILED`` (a confirmed fact);
    * ``unknown``: ``CREATED -> UNKNOWN`` and ``EXECUTION_PENDING -> PAYMENT_UNKNOWN``;
      the reservation is *held* -- ``PAYMENT_UNKNOWN`` deliberately has no edge to
      ``CANCELLED`` or ``EXPIRED``, because the order may exist and the money may move.

    Idempotent: a redelivered command that finds the attempt already in the state this
    outcome produces (with the same order id) returns a no-op transition. Any other
    state raises :class:`PaymentStateConflictError`: the step is ``CREATED ->`` and a
    concurrent actor already moved the row.

    The checkout is moved only when the table allows it from its current state; if a
    cancel raced ahead, the attempt still records the provider fact and the audit row
    names the checkout state that was kept.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    if not isinstance(outcome, ProviderOrderOutcome):
        raise PaymentsUsageError("outcome must be a ProviderOrderOutcome")

    target = {
        "ok": PaymentState.SUBMITTED,
        "failed": PaymentState.FAILED,
        "unknown": PaymentState.UNKNOWN,
    }[outcome.kind]
    attempt, version = _lock_version_and_attempt(
        session, tenant_id, attempt_id, lock_reservation=outcome.kind == "failed"
    )
    before = attempt.status

    if before is target and (
        outcome.kind != "ok" or attempt.provider_order_id == outcome.provider_order_id
    ):
        return AttemptTransition(before, before, version.status)
    if before is not PaymentState.CREATED:
        raise PaymentStateConflictError(before, target, step="record_create_order_result")
    assert_transition(before, target)
    _move_attempt(session, attempt, target, provider_order_id=outcome.provider_order_id)

    checkout_target = {
        "ok": CheckoutState.AWAITING_PAYMENT,
        "failed": CheckoutState.PAYMENT_FAILED,
        "unknown": CheckoutState.PAYMENT_UNKNOWN,
    }[outcome.kind]
    checkout_after = _move_checkout_if_legal(session, tenant_id, attempt, version, checkout_target)

    release_code: str | None = None
    if outcome.kind == "failed":
        released = reservations.release(
            session,
            checkout_id=attempt.checkout_id,
            checkout_version=attempt.checkout_version,
            cause=reservations.ReleaseCause.PAYMENT_FAILED,
        )
        release_code = released.code.value

    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="payment.order_result_recorded",
        actor_type=ActorType.WORKER,
        payload={
            "kind": outcome.kind,
            "code": outcome.code.value,
            "reason": outcome.reason,
            "provider_order_id": outcome.provider_order_id,
            "state_before": before.value,
            "state_after": target.value,
            "checkout_state_before": version.status.value,
            "checkout_state_after": checkout_after.value,
            "reservation_release": release_code,
        },
        correlation_id=correlation_id,
    )
    return AttemptTransition(before, target, checkout_after)


def record_recovered_order(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    provider_order_id: str,
    correlation_id: uuid.UUID,
) -> AttemptTransition:
    """Reconciliation found the order by receipt after a lost create response (10.6, 10.7).

    The attempt **stays** ``RECONCILING``: finding the order says nothing about the
    payment, and the next reconciliation step is to fetch the order's payments. Only the
    provider order id is stored (refusing, as above, an id already bound elsewhere).

    The checkout moves ``EXECUTION_PENDING -> AWAITING_PAYMENT`` only when it is still in
    ``EXECUTION_PENDING`` -- the single state ``CHECKOUT_TRANSITIONS`` gives an edge to
    ``AWAITING_PAYMENT`` from. In the designed path the checkout is already
    ``PAYMENT_UNKNOWN`` (set when the create call timed out), which has no such edge, so
    it stays there until verified evidence resolves it to ``PAID`` or ``PAYMENT_FAILED``.

    Idempotent for the same order id; a different order id already recorded raises
    :class:`ProviderOrderConflictError`.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    _require_identifier(provider_order_id, "provider_order_id")
    attempt, version = _lock_version_and_attempt(session, tenant_id, attempt_id)
    if attempt.status is not PaymentState.RECONCILING:
        raise PaymentStateConflictError(
            attempt.status, PaymentState.RECONCILING, step="record_recovered_order"
        )
    if attempt.provider_order_id == provider_order_id:
        return AttemptTransition(attempt.status, attempt.status, version.status)
    if attempt.provider_order_id is not None:
        raise ProviderOrderConflictError(provider_order_id)

    # Same state before and after: the guarded UPDATE only stores the identifier.
    _move_attempt(session, attempt, attempt.status, provider_order_id=provider_order_id)
    checkout_after = _move_checkout_if_legal(
        session, tenant_id, attempt, version, CheckoutState.AWAITING_PAYMENT
    )
    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="payment.order_recovered",
        actor_type=ActorType.WORKER,
        payload={
            "provider_order_id": provider_order_id,
            "receipt": attempt.receipt,
            "state": attempt.status.value,
            "checkout_state_before": version.status.value,
            "checkout_state_after": checkout_after.value,
        },
        correlation_id=correlation_id,
    )
    return AttemptTransition(attempt.status, attempt.status, checkout_after)


# ------------------------------------------------------------- browser callback


def record_browser_callback(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    provider_order_id: str,
    provider_payment_id: str,
    correlation_id: uuid.UUID,
    principal_id: str | None = None,
) -> BrowserCallbackRecorded:
    """Record what the buyer's browser said after Razorpay Checkout closed (ADR D8).

    The API has already verified ``HMAC(order_id|payment_id)``; that proves the caller
    holds the key secret, not that money moved. So this function:

    * checks the order id is the one bound to this attempt -- a callback naming another
      order is refused with ``AUTHORITY_INSUFFICIENT`` and recorded as such;
    * stores the payment id if none is recorded, accepts a repeat of the same id, and
      refuses a *different* id with ``CONCURRENT_OPERATION`` once one is known -- the
      first recorded id is what reconciliation fetches, and a second cannot overwrite
      it from the browser;
    * **never changes the payment state**. ``CAPTURED`` is applied only by
      :func:`apply_provider_evidence` from a webhook or a fetch.

    Audits ``payment.browser_callback`` for every call, accepted or not: a refused
    callback is evidence too.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    _require_identifier(provider_order_id, "provider_order_id")
    _require_identifier(provider_payment_id, "provider_payment_id")
    attempt, _version = _lock_version_and_attempt(session, tenant_id, attempt_id)

    if attempt.provider_order_id != provider_order_id:
        verdict = BrowserCallbackRecorded(
            accepted=False,
            code=RecoveryCode.AUTHORITY_INSUFFICIENT,
            reason="order_not_bound_to_attempt",
            state=attempt.status,
            provider_payment_id=attempt.provider_payment_id,
        )
    elif attempt.provider_payment_id is None:
        _move_attempt(session, attempt, attempt.status, provider_payment_id=provider_payment_id)
        verdict = BrowserCallbackRecorded(
            accepted=True,
            code=RecoveryCode.OK,
            reason="payment_id_recorded",
            state=attempt.status,
            provider_payment_id=provider_payment_id,
        )
    elif attempt.provider_payment_id == provider_payment_id:
        verdict = BrowserCallbackRecorded(
            accepted=True,
            code=RecoveryCode.DUPLICATE_OPERATION,
            reason="payment_id_already_recorded",
            state=attempt.status,
            provider_payment_id=provider_payment_id,
        )
    else:
        verdict = BrowserCallbackRecorded(
            accepted=False,
            code=RecoveryCode.CONCURRENT_OPERATION,
            reason="different_payment_id_already_recorded",
            state=attempt.status,
            provider_payment_id=attempt.provider_payment_id,
        )

    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="payment.browser_callback",
        actor_type=ActorType.BUYER,
        principal_id=principal_id,
        payload={
            "source": EvidenceSource.BROWSER_CALLBACK.value,
            "provider_order_id": provider_order_id,
            "provider_payment_id": provider_payment_id,
            "accepted": verdict.accepted,
            "code": verdict.code.value,
            "reason": verdict.reason,
            "state": attempt.status.value,
        },
        correlation_id=correlation_id,
    )
    return verdict


# ----------------------------------------------------------- provider evidence


def _mismatch_reason(attempt: AttemptView, evidence: ProviderEvidence) -> str | None:
    """Why this evidence is not about this attempt, or ``None`` when it is.

    A settled payment id that differs from the one already recorded is a mismatch even
    though the order, amount and currency agree: two payments holding money on one order
    is a double charge, not a choice to make automatically.
    """
    if evidence.provider_order_id != attempt.provider_order_id:
        return "provider_order_id_mismatch"
    if evidence.amount_minor != attempt.amount.minor:
        return "amount_mismatch"
    if evidence.currency != attempt.amount.currency:
        return "currency_mismatch"
    if (
        evidence.money_held
        and attempt.provider_payment_id is not None
        and attempt.provider_payment_id != evidence.provider_payment_id
    ):
        return "settled_payment_id_conflict"
    return None


def _ensure_order(
    session: Session,
    tenant_id: uuid.UUID,
    attempt: AttemptView,
    version: _VersionRow,
    evidence: ProviderEvidence,
) -> uuid.UUID:
    """Exactly one ``orders`` row per attempt, however many times capture is reported."""
    if version.policy_receipt_id is None or version.policy_receipt_hash is None:
        raise PaymentsUsageError(
            "checkout version has no Policy-at-Sale Receipt bound; an order cannot be "
            "confirmed against a sale whose governing policy is unknown"
        )
    inserted = session.execute(
        _INSERT_ORDER,
        {
            "id": uuid7(),
            "t": tenant_id,
            "m": version.merchant_id,
            "c": attempt.checkout_id,
            "v": attempt.checkout_version,
            "a": attempt.attempt_id,
            "rid": version.policy_receipt_id,
            "rh": version.policy_receipt_hash,
            "total": attempt.amount.minor,
            "cur": attempt.amount.currency,
            "evidence": json.dumps(evidence.as_record(), sort_keys=True),
        },
    ).scalar()
    if inserted is not None:
        return uuid.UUID(str(inserted))
    existing = session.execute(_FIND_ORDER, {"t": tenant_id, "a": attempt.attempt_id}).scalar()
    if existing is None:  # pragma: no cover - ON CONFLICT implies the row exists
        raise PaymentsUsageError("orders row vanished between ON CONFLICT and re-read")
    return uuid.UUID(str(existing))


def apply_provider_evidence(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    evidence: ProviderEvidence,
    correlation_id: uuid.UUID,
) -> EvidenceApplied:
    """Fold one provider fact into the attempt. Idempotent, monotonic, cross-checked.

    Order of work, and the guarantee each step carries:

    1. **Source gate (ADR D8).** ``BROWSER_CALLBACK`` evidence is refused with
       :class:`PaymentsUsageError`; it belongs to :func:`record_browser_callback`.
    2. **Locks (ADR D5).** Locate, lock ``checkout_versions``, lock the reservation when
       the evidence is ``failed`` (a release may follow), lock ``payment_attempts``.
    3. **Cross-check.** The evidence must name this attempt's provider order, amount and
       currency, and must not name a second settled payment. A mismatch never changes
       state *from the evidence*; it escalates the attempt (once -- see :func:`escalate`)
       and audits ``evidence.mismatch``.
    4. **Join.** ``status`` maps ``captured -> CAPTURED``, ``authorized -> AUTHORIZED``,
       ``failed -> FAILED``; ``pending`` is no transition. If the checkout is in a state
       that can never be fulfilled (``INVALIDATED_AWAITING_PAYMENT_RESULT`` and the three
       terminal non-``PAID`` states), a capture is applied as ``STALE_CAPTURE`` instead,
       so the attempt never spends a moment reading ``CAPTURED``. The result is
       ``states.monotonic_apply(current, incoming)``: ``CAPTURED`` never regresses to a
       late ``AUTHORIZED``, a duplicate is a no-op with ``changed=False``, a terminal
       attempt absorbs everything, and ``UNKNOWN`` moves only to ``RECONCILING``.
    5. **Consequences.**

       * ``CAPTURED`` with sufficient evidence (:func:`evidence.may_fulfil`): exactly one
         ``orders`` row, ``INSERT ... ON CONFLICT (tenant_id, payment_attempt_id) DO
         NOTHING`` carrying the evidence as ``capture_evidence``, and the checkout moves
         to ``PAID`` (``AWAITING_PAYMENT -> PAID`` or ``PAYMENT_UNKNOWN -> PAID``);
       * ``STALE_CAPTURE``: **no order row**; the checkout is left for the refund path;
       * ``FAILED``: checkout ``-> PAYMENT_FAILED`` (or ``INVALIDATED_AWAITING_PAYMENT_RESULT
         -> INVALIDATED``, the result being known) and the reservation is released with
         cause ``PAYMENT_FAILED``;
       * ``AUTHORIZED``: checkout unchanged; the payment id is recorded.

    A failed report naming a payment other than the one already recorded on the attempt is
    ignored (``changed=False``): a first try that failed does not end the attempt whose
    second try is the payment we hold.

    Audits ``payment.evidence_applied`` on every call, carrying source, event id,
    ``state_before`` and ``state_after`` -- duplicates included, because evidence received
    is a fact worth keeping even when it changes nothing.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    if not isinstance(evidence, ProviderEvidence):
        raise PaymentsUsageError("evidence must be a transaction_kernel.evidence.ProviderEvidence")
    if evidence.source is EvidenceSource.BROWSER_CALLBACK:
        raise PaymentsUsageError(
            "a browser callback is never provider evidence (ADR D8); record it with "
            "record_browser_callback and reconcile from a fetch"
        )

    from .payment_window import close_due

    close_due(session, attempt_id, correlation_id=correlation_id)
    attempt, version = _lock_version_and_attempt(
        session, tenant_id, attempt_id, lock_reservation=evidence.status == "failed"
    )
    before = attempt.status
    if attempt.provider_order_id is None:
        raise PaymentsUsageError(
            f"attempt {attempt_id} has no provider order recorded; evidence cannot be "
            "cross-checked against it (record the create-order result or the recovered "
            "order first)"
        )

    base_payload: dict[str, Any] = {
        "source": evidence.source.value,
        "event_id": evidence.event_id,
        "provider_payment_id": evidence.provider_payment_id,
        "provider_order_id": evidence.provider_order_id,
        "status": evidence.status,
        "provider_status": evidence.provider_status,
        "amount_minor": evidence.amount_minor,
        "currency": evidence.currency,
        "raw_digest": evidence.raw_digest,
        "state_before": before.value,
        "checkout_state_before": version.status.value,
    }

    mismatch = _mismatch_reason(attempt, evidence)
    if mismatch is not None:
        escalation = _escalate_locked(
            session,
            tenant_id=tenant_id,
            attempt=attempt,
            reason="evidence_mismatch",
            correlation_id=correlation_id,
        )
        _audit(
            session,
            tenant_id=tenant_id,
            attempt=attempt,
            event_type="evidence.mismatch",
            actor_type=ActorType.WORKER,
            payload={
                **base_payload,
                "mismatch": mismatch,
                "expected_provider_order_id": attempt.provider_order_id,
                "expected_amount_minor": attempt.amount.minor,
                "expected_currency": attempt.amount.currency,
                "recorded_provider_payment_id": attempt.provider_payment_id,
                "state_after": escalation.state_after.value,
                "case_key": escalation.case_key,
            },
            correlation_id=correlation_id,
        )
        return EvidenceApplied(
            state_before=before,
            state_after=escalation.state_after,
            changed=escalation.state_before is not escalation.state_after,
            order_id=None,
            stale_capture=False,
            reason=mismatch,
            checkout_state_after=version.status,
            case_key=escalation.case_key,
            refund_reported=evidence.refund_reported,
        )

    incoming = evidence.payment_state
    stale_context = version.status in _NEVER_FULFIL
    reason: str
    if incoming is None:
        after = before
        reason = "pending_no_transition"
    elif (
        incoming is PaymentState.FAILED
        and attempt.provider_payment_id is not None
        and attempt.provider_payment_id != evidence.provider_payment_id
    ):
        after = before
        reason = "failed_report_names_other_payment"
    else:
        if incoming is PaymentState.CAPTURED and stale_context:
            incoming = PaymentState.STALE_CAPTURE
        after = monotonic_apply(before, incoming)
        if after is before:
            reason = "duplicate_or_weaker_evidence"
        elif after is incoming:
            reason = f"applied_{after.value.lower()}"
        else:
            reason = f"joined_to_{after.value.lower()}"

    changed = after is not before
    record_payment_id = evidence.provider_payment_id if evidence.money_held else None
    if changed or (record_payment_id is not None and attempt.provider_payment_id is None):
        _move_attempt(session, attempt, after, provider_payment_id=record_payment_id)

    order_id: uuid.UUID | None = None
    checkout_after = version.status
    release_code: str | None = None
    if after is PaymentState.CAPTURED and may_fulfil(after, evidence.channel):
        order_id = _ensure_order(session, tenant_id, attempt, version, evidence)
        checkout_after = _move_checkout_if_legal(
            session, tenant_id, attempt, version, CheckoutState.PAID
        )
    elif after is PaymentState.FAILED and changed:
        target = (
            CheckoutState.INVALIDATED
            if version.status is CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT
            else CheckoutState.PAYMENT_FAILED
        )
        checkout_after = _move_checkout_if_legal(session, tenant_id, attempt, version, target)
        released = reservations.release(
            session,
            checkout_id=attempt.checkout_id,
            checkout_version=attempt.checkout_version,
            cause=reservations.ReleaseCause.PAYMENT_FAILED,
        )
        release_code = released.code.value

    stale_capture = after is PaymentState.STALE_CAPTURE
    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="payment.evidence_applied",
        actor_type=ActorType.WORKER,
        payload={
            **base_payload,
            "state_after": after.value,
            "changed": changed,
            "reason": reason,
            "order_id": None if order_id is None else str(order_id),
            "stale_capture": stale_capture,
            "refund_reported": evidence.refund_reported,
            "amount_refunded_minor": evidence.amount_refunded_minor,
            "checkout_state_after": checkout_after.value,
            "reservation_release": release_code,
        },
        correlation_id=correlation_id,
    )
    return EvidenceApplied(
        state_before=before,
        state_after=after,
        changed=changed,
        order_id=order_id,
        stale_capture=stale_capture,
        reason=reason,
        checkout_state_after=checkout_after,
        refund_reported=evidence.refund_reported,
    )


# --------------------------------------------------------------- reconciliation


def begin_reconciling(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    correlation_id: uuid.UUID,
) -> bool:
    """``UNKNOWN -> RECONCILING`` (or ``REFUND_UNKNOWN -> RECONCILING``), exactly once.

    Specification 10.7: an unknown outcome leaves only through reconciliation, and this
    is that single edge. Returns ``True`` when this call made the move and ``False`` when
    the attempt was already ``RECONCILING`` (a redelivered command). Any other state
    raises :class:`PaymentStateConflictError`. The checkout stays ``PAYMENT_UNKNOWN`` and
    the reservation stays held; neither is resolved by the decision to go and look.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    attempt, version = _lock_version_and_attempt(session, tenant_id, attempt_id)
    if attempt.status is PaymentState.RECONCILING:
        return False
    if attempt.status not in (PaymentState.UNKNOWN, PaymentState.REFUND_UNKNOWN):
        raise PaymentStateConflictError(
            attempt.status, PaymentState.RECONCILING, step="begin_reconciling"
        )
    assert_transition(attempt.status, PaymentState.RECONCILING)
    _move_attempt(session, attempt, PaymentState.RECONCILING)
    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="payment.reconciliation_started",
        actor_type=ActorType.WORKER,
        payload={
            "state_before": attempt.status.value,
            "state_after": PaymentState.RECONCILING.value,
            "checkout_state": version.status.value,
            "receipt": attempt.receipt,
            "provider_order_id": attempt.provider_order_id,
        },
        correlation_id=correlation_id,
    )
    return True


def record_reconciliation_run(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    attempt_number: int,
    reason: str,
    identifiers_queried: Mapping[str, str | None],
    decision: str,
    correlation_id: uuid.UUID,
    refund_id: uuid.UUID | None = None,
    raw_evidence_digest: str | None = None,
    resulting_transition: str | None = None,
    next_attempt_in_seconds: int | None = None,
) -> uuid.UUID | None:
    """Record one bounded reconciliation attempt; ``None`` if it was already recorded.

    ``UNIQUE (tenant_id, payment_attempt_id, reason, attempt_number)`` is what keeps
    "6 attempts then ESCALATED" (ADR D13) an honest count under redelivery: the violation
    is caught under a SAVEPOINT and reported as ``None`` so the caller's transaction
    survives and the count does not advance. ``attempt_number`` above
    :data:`RECONCILIATION_ATTEMPT_BOUND` is refused outright -- a seventh run means the
    bound was not applied, and recording it would make the evidence lie about the bound.

    ``next_attempt_in_seconds`` is turned into a timestamp by the database clock.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int):
        raise PaymentsUsageError("attempt_number must be an int")
    if not 1 <= attempt_number <= RECONCILIATION_ATTEMPT_BOUND:
        raise PaymentsUsageError(
            f"attempt_number must be between 1 and {RECONCILIATION_ATTEMPT_BOUND} (ADR D13), "
            f"got {attempt_number}"
        )
    _require_reason(reason, "reason")
    _require_reason(decision, "decision")
    if resulting_transition is not None and len(resulting_transition) > 64:
        raise PaymentsUsageError("resulting_transition exceeds 64 characters")
    if raw_evidence_digest is not None:
        _require_digest(raw_evidence_digest, "raw_evidence_digest")
    if next_attempt_in_seconds is not None and (
        isinstance(next_attempt_in_seconds, bool)
        or not isinstance(next_attempt_in_seconds, int)
        or next_attempt_in_seconds < 0
    ):
        raise PaymentsUsageError("next_attempt_in_seconds must be a non-negative int or None")
    identifiers = {str(k): (None if v is None else str(v)) for k, v in identifiers_queried.items()}

    attempt = _read_attempt_unlocked(session, tenant_id, attempt_id)
    run_id = uuid7()
    try:
        with session.begin_nested():
            session.execute(
                _INSERT_RECONCILIATION_RUN,
                {
                    "id": run_id,
                    "t": tenant_id,
                    "a": attempt_id,
                    "r": refund_id,
                    "n": attempt_number,
                    "reason": reason,
                    "identifiers": json.dumps(identifiers, sort_keys=True),
                    "digest": raw_evidence_digest,
                    "decision": decision,
                    "transition": resulting_transition,
                    "delay": next_attempt_in_seconds,
                    "corr": correlation_id,
                },
            )
    except IntegrityError as exc:
        if _is_violation_of(exc, _RECONCILIATION_RUN_UNIQUE):
            return None
        raise

    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="payment.reconciliation_run_recorded",
        actor_type=ActorType.WORKER,
        payload={
            "reconciliation_run_id": str(run_id),
            "refund_id": None if refund_id is None else str(refund_id),
            "attempt_number": attempt_number,
            "bound": RECONCILIATION_ATTEMPT_BOUND,
            "reason": reason,
            "identifiers_queried": identifiers,
            "raw_evidence_digest": raw_evidence_digest,
            "decision": decision,
            "resulting_transition": resulting_transition,
            "next_attempt_in_seconds": next_attempt_in_seconds,
        },
        correlation_id=correlation_id,
    )
    return run_id


# ------------------------------------------------------------------- escalation


def _case_key(tenant_id: uuid.UUID, attempt_id: uuid.UUID, reason: str) -> str:
    """Specification 6.4.3: a deterministic key so concurrent detectors share one case."""
    return canonical_hash(
        {
            "tenant_id": str(tenant_id),
            "payment_attempt_id": str(attempt_id),
            "reason_family": reason,
        }
    )


def _escalate_locked(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    attempt: AttemptView,
    reason: str,
    correlation_id: uuid.UUID,
) -> Escalation:
    """Escalate an attempt whose version and row are already locked by this transaction."""
    key = _case_key(tenant_id, attempt.attempt_id, reason)
    before = attempt.status
    if before is PaymentState.ESCALATED:
        return Escalation(key, before, before, opened=False)

    path = _ESCALATION_PATH.get(before)
    if path is None:
        # Money has moved or the attempt is finished: the row cannot be frozen, so the
        # case is the whole outcome. The audit stream (read under the version lock, which
        # every escalator holds) says whether it is already open.
        exists = session.execute(
            _CASE_EXISTS,
            {"t": tenant_id, "agg": _AGGREGATE, "c": attempt.checkout_id, "k": key},
        ).scalar()
        if exists:
            return Escalation(key, before, before, opened=False)
        after: PaymentState = before
    else:
        current: PaymentState = before
        for hop in path:
            assert_transition(current, hop)
            current = hop
        after = PaymentState.ESCALATED
        _move_attempt(session, attempt, after)

    _audit(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        event_type="human_review.opened",
        actor_type=ActorType.SYSTEM,
        payload={
            "case_key": key,
            "reason_family": reason,
            "state_before": before.value,
            "state_after": after.value,
            "path": [s.value for s in (path or ())],
            "provider_order_id": attempt.provider_order_id,
            "provider_payment_id": attempt.provider_payment_id,
            "amount_minor": attempt.amount.minor,
            "currency": attempt.amount.currency,
        },
        correlation_id=correlation_id,
    )
    return Escalation(key, before, after, opened=True)


def escalate(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_attempt_id: uuid.UUID,
    reason: str,
    correlation_id: uuid.UUID,
) -> Escalation:
    """Open exactly one human review case for an attempt and freeze it if it can be frozen.

    Specification 6.4.3: three components can detect the same stuck payment, and a
    reviewer must not work the same evidence three times. The case key is the canonical
    hash of ``(tenant, attempt, reason_family)``; the version row lock serializes
    concurrent detectors, and the second one finds the row ``ESCALATED`` (or the case
    already in the audit stream) and returns the same key with ``opened=False``.

    The state moves to ``ESCALATED`` through the only legal route the transition table
    offers -- ``RECONCILING -> ESCALATED`` and ``REFUND_FAILED -> ESCALATED`` directly;
    ``SUBMITTED``/``AUTHORIZED``/``CREATED`` via ``UNKNOWN -> RECONCILING`` and
    ``UNKNOWN``/``REFUND_UNKNOWN`` via ``RECONCILING`` -- each hop asserted, one guarded
    UPDATE. ``ESCALATED`` is terminal: no automated edge leaves it, so a replayed webhook
    or a retry loop cannot thaw a frozen attempt. An attempt in which money has moved
    (``CAPTURED``, ``STALE_CAPTURE``, the refund postures) or that is already terminal is
    not frozen; the case is opened without a state change.

    Audits ``human_review.opened`` once per case key.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    attempt_id = _require_uuid(payment_attempt_id, "payment_attempt_id")
    _require_reason(reason, "reason")
    attempt, _version = _lock_version_and_attempt(session, tenant_id, attempt_id)
    return _escalate_locked(
        session,
        tenant_id=tenant_id,
        attempt=attempt,
        reason=reason,
        correlation_id=correlation_id,
    )


# ------------------------------------------------------------------ webhook inbox


def record_webhook_applied(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    inbox_id: uuid.UUID,
    apply_status: str,
    apply_reason: str | None,
    state_before: PaymentState | None,
    state_after: PaymentState | None,
    changed: bool | None,
    outbox_command_id: uuid.UUID | None,
    correlation_id: uuid.UUID,
) -> None:
    """Stamp the apply outcome on a ``webhook_inbox`` row (ADR D7).

    ``apply_status`` is ``APPLIED``, ``IGNORED`` or ``FAILED`` -- never back to
    ``RECEIVED``. The state change itself was audited by :func:`apply_provider_evidence`;
    this row is the receiver-side record that the delivery was processed and what it did,
    and it is audited on its own stream (``webhook_inbox``) so the inspector can show it.
    Raises :class:`InboxRowNotFoundError` when no row is visible to this tenant.
    """
    _bound_tenant(session, tenant_id, correlation_id)
    _require_uuid(inbox_id, "inbox_id")
    if apply_status not in ("APPLIED", "IGNORED", "FAILED"):
        raise PaymentsUsageError(
            f"apply_status must be APPLIED, IGNORED or FAILED, got {apply_status!r}"
        )
    if apply_reason is not None:
        _require_reason(apply_reason, "apply_reason")
    row = session.execute(
        _UPDATE_INBOX,
        {
            "status": apply_status,
            "reason": apply_reason,
            "before": None if state_before is None else state_before.value,
            "after": None if state_after is None else state_after.value,
            "changed": changed,
            "cmd": outbox_command_id,
            "t": tenant_id,
            "i": inbox_id,
        },
    ).one_or_none()
    if row is None:
        raise InboxRowNotFoundError(f"webhook inbox row {inbox_id} is not visible to this tenant")
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type="webhook_inbox",
        aggregate_id=inbox_id,
        event_type="webhook.applied",
        actor_type=ActorType.WORKER,
        principal_id=None,
        payload={
            "apply_status": apply_status,
            "apply_reason": apply_reason,
            "state_before": None if state_before is None else state_before.value,
            "state_after": None if state_after is None else state_after.value,
            "changed": changed,
            "outbox_command_id": None if outbox_command_id is None else str(outbox_command_id),
        },
        correlation_id=correlation_id,
    )
