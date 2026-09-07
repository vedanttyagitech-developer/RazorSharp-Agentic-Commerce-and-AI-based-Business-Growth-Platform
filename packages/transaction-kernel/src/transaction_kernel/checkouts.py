"""Checkout lifecycle: head row, immutable versions, cancellation, ADR 0003 D5 and D6.

What this module owns
---------------------
The ``checkouts`` head (identity plus a denormalised pointer to the current version) and
the ``checkout_versions`` rows that are the truth about a sale. Every status change here
passes through :mod:`transaction_kernel.states` -- the table decides what is representable,
this module decides what the locked rows permit -- and every write is audited in the same
transaction, so a version can never be in a state the audit stream cannot explain.

Three rules hold for every function
-----------------------------------
1. **Inside the caller's transaction, under the caller's tenant.** Each function refuses a
   session with no open transaction and a ``tenant_id`` that differs from the one bound
   to it (:func:`require_context`). Row-level security would silently return nothing for
   a foreign tenant; refusing by name keeps a cross-tenant call legible in the evidence.
2. **Lock order is a contract** (ADR D5): ``checkout_versions`` first, then
   ``reservations``, then ``payment_attempts``, then ``execution_grants``. A payment
   attempt is located with a plain SELECT before any lock and locked only after the
   version. :func:`cancel` and the worker's provider-evidence path therefore queue on the
   version row instead of deadlocking.
3. **The version row is the truth; the head mirrors it.** :func:`sync_head` copies the
   current version's status onto the head for cheap reads and never moves the head
   backwards to an older version. A version created outside :func:`create_checkout` (the
   admission fixtures do this) has no head, and that is tolerated: the audit payload
   records whether a head was updated, and readers that need the truth read versions.

Denials
-------
:func:`cancel` returns a structured :class:`CancelResult` for every outcome, including
refusal, because "you cannot cancel while money is in flight" is the system working, not
an error. The other mutators raise typed :class:`CheckoutError` subclasses that carry a
:class:`~transaction_kernel.recovery.RecoveryCode`, never prose a caller has to parse.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from commerce_domain import DomainError, Money, uuid7
from platform_db import require_tenant
from sqlalchemy import CursorResult, Row, TextClause, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, grants, receipts, reservations
from .checkout_content import content_hash, total_of, validate_checkout_content
from .contracts import ActorType, AgentPrincipal, CheckoutRef
from .receipts import BuyerVisibleRef, MerchantPolicy, ReceiptDraft
from .recovery import RecoveryCode
from .states import (
    NON_TERMINAL_PAYMENT_STATES,
    CheckoutState,
    InvalidTransitionError,
    PaymentState,
    assert_transition,
    can_transition,
    is_terminal,
)

__all__ = [
    "AGGREGATE_TYPE",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "ApprovalCard",
    "CancelResult",
    "CheckoutConcurrencyError",
    "CheckoutCreated",
    "CheckoutError",
    "CheckoutHead",
    "CheckoutReservationError",
    "CheckoutStateError",
    "CheckoutTenantError",
    "CheckoutUsageError",
    "CheckoutVersionView",
    "LockedVersion",
    "ReceiptInputs",
    "TransitionResult",
    "apply_transition",
    "cancel",
    "create_checkout",
    "supersede_checkout",
    "current_version",
    "invalidate_open",
    "lock_version",
    "read_head",
    "read_versions",
    "freeze_for_approval",
    "require_context",
    "sync_head",
    "transition",
]

#: Audit stream every checkout event is appended to, keyed by ``checkout_id``. The same
#: stream admission writes to, so one chain tells the whole story of a sale.
AGGREGATE_TYPE: Final = "checkout"

#: ADR D13. Long enough for a buyer to read an approval card and decide; short enough
#: that abandoned checkouts do not withhold stock from everyone else for long.
DEFAULT_RESERVATION_TTL_SECONDS: Final = 900

#: Name of the unique constraint on ``checkouts (tenant_id, cart_id)``. Matched by name
#: so an unrelated integrity failure is never reported as a duplicate checkout.
_ONE_CHECKOUT_PER_BASKET: Final = "uq_checkouts_tenant_id_cart_id"
_ONE_CHECKOUT_PER_BASKET_LEGACY: Final = "uq_checkouts_tenant_id_basket_id"
_UNIQUE_VIOLATION: Final = "23505"


# --------------------------------------------------------------------------- errors


class CheckoutError(DomainError):
    """A checkout operation was refused. ``code`` is what the caller may do next."""

    code: RecoveryCode = RecoveryCode.POLICY_EXCEPTION

    def __init__(self, reason: str, message: str, *, code: RecoveryCode | None = None) -> None:
        self.reason = reason
        if code is not None:
            self.code = code
        super().__init__(f"{reason}: {message}")


class CheckoutUsageError(CheckoutError):
    """The caller used this module wrongly: no transaction, a malformed argument."""


class CheckoutTenantError(CheckoutError):
    """The caller named a tenant other than the one bound to this transaction."""

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class CheckoutStateError(CheckoutError):
    """The locked rows do not permit the requested move.

    ``current`` and ``target`` are filled in for an illegal transition so the audit trail
    can say which edge was refused, not merely that one was.
    """

    code = RecoveryCode.STALE_CHECKOUT

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        code: RecoveryCode | None = None,
        current: CheckoutState | None = None,
        target: CheckoutState | None = None,
    ) -> None:
        super().__init__(reason, message, code=code)
        self.current = current
        self.target = target


class CheckoutReservationError(CheckoutError):
    """The hold that an approval requires could not be taken. Carries the reservation
    module's own code (STALE_CHECKOUT for insufficient stock, CONCURRENT_OPERATION when
    other live holds took it) so the caller's recovery is the one the hold decided."""


class CheckoutConcurrencyError(CheckoutError):
    """A guarded write found the row changed under a lock this transaction holds.

    Unreachable while every writer honours ADR D5; raised rather than ignored so that a
    future writer which breaks the lock order fails closed instead of half-cancelling.
    """

    code = RecoveryCode.CONCURRENT_OPERATION


# --------------------------------------------------------------------------- values


@dataclass(frozen=True, slots=True)
class ReceiptInputs:
    """What the merchant contributes to a Policy-at-Sale Receipt.

    The kernel adds the parts it owns -- tenant, merchant, checkout, version, content hash,
    correlation -- when it builds the :class:`~transaction_kernel.receipts.ReceiptDraft`.
    """

    policies: tuple[MerchantPolicy, ...]
    tax_policy_version: str
    rounding_policy_version: str
    buyer_visible_refs: tuple[BuyerVisibleRef, ...]
    store_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CheckoutCreated:
    """Version 1 exists and is QUOTED."""

    checkout_id: uuid.UUID
    version: int
    content_hash: str
    total: Money

    @property
    def ref(self) -> CheckoutRef:
        return CheckoutRef(self.checkout_id, self.version, self.content_hash)


@dataclass(frozen=True, slots=True)
class ApprovalCard:
    """What the trusted surface shows the buyer: the exact bytes, terms and deadline."""

    checkout: CheckoutRef
    receipt_id: uuid.UUID
    receipt_hash: str
    total: Money
    reservation_expires_at: datetime
    merchant_id: uuid.UUID
    content: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TransitionResult:
    checkout: CheckoutRef
    from_state: CheckoutState
    to_state: CheckoutState
    head_updated: bool


@dataclass(frozen=True, slots=True)
class CancelResult:
    """The answer to a cancellation request. Always structured, never prose.

    ``explanation`` is a stable reason key. ``allowed`` false with a code is a normal
    outcome: a checkout whose payment surface is open cannot be cancelled outright
    (specification 10.8), and saying so is the kernel doing its job.
    """

    allowed: bool
    code: RecoveryCode
    explanation: str
    checkout: CheckoutRef
    from_state: CheckoutState | None = None
    grants_revoked: tuple[uuid.UUID, ...] = ()
    attempt_expired: uuid.UUID | None = None
    reservation_release: RecoveryCode | None = None

    def __post_init__(self) -> None:
        if self.allowed != (self.code is RecoveryCode.OK):
            raise ValueError("allowed and RecoveryCode.OK must agree")


@dataclass(frozen=True, slots=True)
class CheckoutHead:
    """The ``checkouts`` row: identity and a mirror of the current version's state."""

    checkout_id: uuid.UUID
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    cart_id: uuid.UUID
    buyer_ref: str
    current_version: int
    status: CheckoutState
    correlation_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CheckoutVersionView:
    """One immutable version as stored. ``content`` is a copy, never the ORM attribute."""

    checkout_id: uuid.UUID
    merchant_id: uuid.UUID
    version: int
    status: CheckoutState
    content_hash: str
    content: Mapping[str, Any]
    total: Money
    policy_receipt_id: uuid.UUID | None
    policy_receipt_hash: str | None
    immutable: bool
    created_at: datetime
    invalidated_at: datetime | None

    @property
    def ref(self) -> CheckoutRef:
        return CheckoutRef(self.checkout_id, self.version, self.content_hash)


@dataclass(frozen=True, slots=True)
class LockedVersion:
    """A version row held ``FOR UPDATE`` by this transaction."""

    checkout_id: uuid.UUID
    merchant_id: uuid.UUID
    version: int
    status: CheckoutState
    content_hash: str
    content: Mapping[str, Any]
    currency: str
    total_minor: int
    policy_receipt_id: uuid.UUID | None
    policy_receipt_hash: str | None
    immutable: bool
    invalidated_at: datetime | None

    @property
    def total(self) -> Money:
        return Money(self.total_minor, self.currency)

    @property
    def ref(self) -> CheckoutRef:
        return CheckoutRef(self.checkout_id, self.version, self.content_hash)


# ------------------------------------------------------------------------------- SQL

# Every statement is composed from literals in this module. Nothing from a request, a
# model or a database row is ever joined into SQL text; every variable is a bound parameter.

_VERSION_COLUMNS: Final = (
    "checkout_id, merchant_id, version, status, content, content_hash, currency, "
    "total_minor, policy_receipt_id, policy_receipt_hash, immutable, created_at, "
    "invalidated_at"
)


def _stmt(*parts: str) -> TextClause:
    """Compose one statement from the SQL fragments in this module.

    Every argument is a literal written here or a module-level constant; nothing from a
    request, a model or a row is ever joined into SQL text, and every variable value
    travels as a bound parameter.
    """
    return text(" ".join(parts))


_LOCK_VERSION = _stmt(
    "SELECT",
    _VERSION_COLUMNS,
    "FROM checkout_versions WHERE tenant_id = :t AND checkout_id = :c AND version = :v",
    "FOR UPDATE",
)

_SELECT_VERSIONS = _stmt(
    "SELECT",
    _VERSION_COLUMNS,
    "FROM checkout_versions WHERE tenant_id = :t AND checkout_id = :c ORDER BY version",
)

_SELECT_CURRENT_VERSION = _stmt(
    "SELECT",
    _VERSION_COLUMNS,
    "FROM checkout_versions WHERE tenant_id = :t AND checkout_id = :c",
    "ORDER BY version DESC LIMIT 1",
)

# Guarded on the old status so a concurrent writer that slipped past the lock produces a
# zero-row update rather than a silent overwrite. ``invalidated_at`` is stamped only for
# the two invalidation targets and never cleared: admission's supersede path stamps it
# too, and a row that carries it can never be revived whatever its status says.
_SET_STATUS = text(
    "UPDATE checkout_versions SET status = :new, "
    "invalidated_at = CASE WHEN CAST(:stamp AS boolean) "
    "THEN COALESCE(invalidated_at, now()) ELSE invalidated_at END "
    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v AND status = :old"
)

_SET_IMMUTABLE = text(
    "UPDATE checkout_versions SET immutable = true "
    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
)

# Never moves the head to an older version: cancelling N after N+1 exists must not make
# the head claim N is current. ``updated_at`` is set here because no trigger exists.
_SYNC_HEAD = text(
    "UPDATE checkouts SET status = :s, current_version = :v, updated_at = now() "
    "WHERE tenant_id = :t AND id = :c AND current_version <= :v"
)

_SELECT_HEAD = text(
    "SELECT id, tenant_id, merchant_id, cart_id, buyer_ref, current_version, status, "
    "correlation_id, created_at, updated_at FROM checkouts WHERE tenant_id = :t AND id = :c"
)

_SELECT_BASKET = text(
    "SELECT merchant_id, buyer_ref, status FROM carts WHERE tenant_id = :t AND id = :b"
)

_INSERT_HEAD = text(
    "INSERT INTO checkouts (id, tenant_id, merchant_id, cart_id, buyer_ref, "
    "current_version, status, correlation_id) "
    "VALUES (:id, :t, :m, :b, :buyer, 1, :status, :corr)"
)

_SELECT_VERSION_STATUS = text(
    "SELECT status FROM checkout_versions "
    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
)

#: Version N may be superseded by N+1 only from a state that can never be spent again.
#:
#: Not ``PAYMENT_FAILED``: the state table gives it an edge back to ``EXECUTION_PENDING``
#: for a policy-safe retry, so a failed payment is a version the buyer may still complete,
#: and building N+1 over it would leave two versions a press could pay for.
_SUPERSEDABLE_FROM: Final[frozenset[CheckoutState]] = frozenset(
    {CheckoutState.INVALIDATED, CheckoutState.CANCELLED, CheckoutState.EXPIRED}
)

_LOCK_HEAD = text(
    "SELECT id, tenant_id, merchant_id, cart_id, buyer_ref, current_version, status "
    "FROM checkouts WHERE tenant_id = :t AND id = :c FOR UPDATE"
)

_INSERT_VERSION = text(
    "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, version, "
    "content, content_hash, currency, total_minor, status, immutable) "
    "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, :cur, :total, :status, false)"
)

# Plain SELECT: ADR D5 locates an attempt without a lock and locks it only after the
# version row. ``NON_TERMINAL_PAYMENT_STATES`` is bound as an array parameter.
_FIND_LIVE_ATTEMPT = text(
    "SELECT id, status FROM payment_attempts "
    "WHERE tenant_id = :t AND checkout_id = :c AND checkout_version = :v "
    "AND status = ANY(:states) ORDER BY created_at DESC LIMIT 1"
)

_LOCK_ATTEMPT = text(
    "SELECT id, status FROM payment_attempts WHERE tenant_id = :t AND id = :id FOR UPDATE"
)

_EXPIRE_ATTEMPT = text(
    "UPDATE payment_attempts SET status = :expired, updated_at = now() "
    "WHERE tenant_id = :t AND id = :id AND status = :created"
)

_FIND_ISSUED_GRANTS = text(
    "SELECT id FROM execution_grants WHERE tenant_id = :t AND checkout_id = :c "
    "AND checkout_version = :v AND status = 'ISSUED' ORDER BY issued_at"
)


# ---------------------------------------------------------------------------- guards


def require_context(session: Session, tenant_id: uuid.UUID) -> uuid.UUID:
    """The transaction and tenant every function here needs, or a typed refusal.

    The tenant argument is compared against the GUC rather than trusted: a caller that
    could name any tenant would be a caller that could act on any tenant.
    """
    if not isinstance(session, Session) or not session.in_transaction():
        raise CheckoutUsageError(
            "no_transaction",
            "checkout operations run inside the caller's open transaction; their locks "
            "and their audit rows exist only for the life of one",
        )
    if not isinstance(tenant_id, uuid.UUID):
        raise CheckoutUsageError("bad_tenant", f"tenant_id must be a UUID, got {tenant_id!r}")
    bound: uuid.UUID = require_tenant(session)
    if bound != tenant_id:
        raise CheckoutTenantError(
            "tenant_mismatch",
            f"caller named tenant {tenant_id} but this transaction is bound to {bound}",
        )
    return bound


def _require_correlation(correlation_id: uuid.UUID) -> uuid.UUID:
    if not isinstance(correlation_id, uuid.UUID):
        raise CheckoutUsageError("bad_correlation_id", "correlation_id must be a UUID")
    return correlation_id


def _content_copy(value: Any) -> dict[str, Any]:
    """Detach stored JSONB from the driver's row so a caller cannot mutate our read."""
    copied: dict[str, Any] = json.loads(json.dumps(value))
    return copied


def _locked(row: Row[Any]) -> LockedVersion:
    return LockedVersion(
        checkout_id=row.checkout_id,
        merchant_id=row.merchant_id,
        version=int(row.version),
        status=CheckoutState(row.status),
        content_hash=str(row.content_hash),
        content=_content_copy(row.content),
        currency=str(row.currency),
        total_minor=int(row.total_minor),
        policy_receipt_id=row.policy_receipt_id,
        policy_receipt_hash=None
        if row.policy_receipt_hash is None
        else str(row.policy_receipt_hash),
        immutable=bool(row.immutable),
        invalidated_at=row.invalidated_at,
    )


def _view(row: Row[Any]) -> CheckoutVersionView:
    return CheckoutVersionView(
        checkout_id=row.checkout_id,
        merchant_id=row.merchant_id,
        version=int(row.version),
        status=CheckoutState(row.status),
        content_hash=str(row.content_hash),
        content=_content_copy(row.content),
        total=Money(int(row.total_minor), str(row.currency)),
        policy_receipt_id=row.policy_receipt_id,
        policy_receipt_hash=None
        if row.policy_receipt_hash is None
        else str(row.policy_receipt_hash),
        immutable=bool(row.immutable),
        created_at=row.created_at,
        invalidated_at=row.invalidated_at,
    )


def _lock_row(
    session: Session, tenant_id: uuid.UUID, checkout: CheckoutRef
) -> LockedVersion | None:
    row = session.execute(
        _LOCK_VERSION, {"t": tenant_id, "c": checkout.checkout_id, "v": checkout.version}
    ).one_or_none()
    return None if row is None else _locked(row)


def lock_version(
    session: Session, *, tenant_id: uuid.UUID, checkout: CheckoutRef, verify_hash: bool = True
) -> LockedVersion:
    """Take ``checkout_versions FOR UPDATE`` -- the first lock of ADR D5 -- and read it.

    Refuses a version that does not exist for this tenant (``STALE_CHECKOUT``: another
    tenant's version is indistinguishable from none, by design) and, unless
    ``verify_hash`` is false, a caller whose ``content_hash`` differs from the stored one:
    a (checkout, version) pair has exactly one hash forever, so a mismatch means the
    caller holds bytes the buyer never approved.
    """
    require_context(session, tenant_id)
    locked = _lock_row(session, tenant_id, checkout)
    if locked is None:
        raise CheckoutStateError(
            "version_missing",
            f"checkout {checkout.checkout_id} version {checkout.version} is not visible to "
            "this tenant",
        )
    if verify_hash and locked.content_hash != checkout.content_hash:
        raise CheckoutStateError(
            "hash_mismatch",
            "the caller's content_hash is not the stored hash of this version; refusing to "
            "act on bytes the buyer did not see",
        )
    return locked


def apply_transition(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    current: CheckoutState,
    target: CheckoutState,
) -> None:
    """One deliberate, locked step of the checkout machine.

    The caller must already hold the row ``FOR UPDATE`` (see :func:`lock_version`).
    :func:`~transaction_kernel.states.assert_transition` decides whether the edge exists;
    the guarded UPDATE decides whether the row still holds ``current``. An illegal edge is
    a :class:`CheckoutStateError` carrying both states; a changed row under our own lock
    is a :class:`CheckoutConcurrencyError`, which cannot happen while D5 holds.
    """
    require_context(session, tenant_id)
    try:
        assert_transition(current, target)
    except InvalidTransitionError as exc:
        raise CheckoutStateError(
            "illegal_transition",
            str(exc),
            current=current,
            target=target,
        ) from exc
    stamp = target in (
        CheckoutState.INVALIDATED,
        CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT,
    )
    result = cast(
        "CursorResult[Any]",
        session.execute(
            _SET_STATUS,
            {
                "new": target.value,
                "old": current.value,
                "stamp": stamp,
                "t": tenant_id,
                "c": checkout.checkout_id,
                "v": checkout.version,
            },
        ),
    )
    if result.rowcount != 1:
        raise CheckoutConcurrencyError(
            "version_changed_under_lock",
            f"checkout {checkout.checkout_id} version {checkout.version} no longer holds "
            f"{current.value}; a writer bypassed the version lock",
        )


def sync_head(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    version: int,
    status: CheckoutState,
) -> bool:
    """Mirror a version's status onto the head. Returns whether a head row was updated.

    False is not an error: versions seeded outside :func:`create_checkout` have no head,
    and the version row remains the truth either way. The result goes into the audit
    payload so a reader can tell the two cases apart later.
    """
    require_context(session, tenant_id)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            _SYNC_HEAD, {"s": status.value, "v": version, "t": tenant_id, "c": checkout_id}
        ),
    )
    return bool(result.rowcount == 1)


def _find_live_attempt(
    session: Session, tenant_id: uuid.UUID, checkout: CheckoutRef
) -> tuple[uuid.UUID, PaymentState] | None:
    row = session.execute(
        _FIND_LIVE_ATTEMPT,
        {
            "t": tenant_id,
            "c": checkout.checkout_id,
            "v": checkout.version,
            "states": [state.value for state in sorted(NON_TERMINAL_PAYMENT_STATES)],
        },
    ).one_or_none()
    return None if row is None else (row.id, PaymentState(row.status))


# ---------------------------------------------------------------------------- create


def create_checkout(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    cart_id: uuid.UUID,
    buyer_ref: str,
    content: Mapping[str, Any],
    correlation_id: uuid.UUID,
    checkout_id: uuid.UUID | None = None,
    principal: AgentPrincipal | None = None,
) -> CheckoutCreated:
    """Open a checkout: head row plus version 1 in ``QUOTED``.

    ``content`` is a canonical document (:mod:`transaction_kernel.checkout_content`); its
    ``checkout_id`` and ``version`` are re-stamped here to the id this function mints (or
    ``checkout_id`` when given) and ``1``, exactly as admission re-stamps N+1, so the
    producer does not have to know the id before pricing the cart. Everything else in
    the document is validated and carried through unchanged into the hash.

    Refuses: a cart that is not visible to this tenant, belongs to another merchant or
    buyer, or is no longer ``OPEN`` (``STALE_CHECKOUT``); a second checkout for one cart
    (``DUPLICATE_OPERATION``, from the unique constraint rather than a pre-read, so two
    concurrent creators cannot both succeed); malformed content
    (:class:`~transaction_kernel.checkout_content.ContentContractError`).

    Does not touch the cart's status: the cart is the API's record, and closing it is
    the API's decision in the same transaction.
    """
    require_context(session, tenant_id)
    _require_correlation(correlation_id)
    if not isinstance(buyer_ref, str) or not buyer_ref or len(buyer_ref) > 128:
        raise CheckoutUsageError("bad_buyer_ref", "buyer_ref must be a non-empty string <= 128")
    if principal is not None and principal.tenant_id != tenant_id:
        raise CheckoutTenantError(
            "principal_tenant_mismatch", "the principal belongs to another tenant"
        )

    cart = session.execute(_SELECT_BASKET, {"t": tenant_id, "b": cart_id}).one_or_none()
    if cart is None:
        raise CheckoutStateError("basket_missing", f"cart {cart_id} is not visible")
    if cart.merchant_id != merchant_id:
        raise CheckoutStateError(
            "basket_merchant_mismatch", "the cart belongs to a different merchant"
        )
    if cart.buyer_ref != buyer_ref:
        raise CheckoutStateError(
            "basket_buyer_mismatch",
            "the cart belongs to a different buyer",
            code=RecoveryCode.AUTHORITY_INSUFFICIENT,
        )
    if cart.status != "OPEN":
        raise CheckoutStateError("basket_not_open", f"cart {cart_id} is {cart.status}")

    new_id = checkout_id if checkout_id is not None else uuid7()
    payload = dict(content)
    payload["checkout_id"] = str(new_id)
    payload["version"] = 1
    validate_checkout_content(payload)
    digest = content_hash(payload)
    total = total_of(payload)

    try:
        # A SAVEPOINT so the unique-constraint refusal below does not poison the caller's
        # transaction; the library never calls session.rollback().
        with session.begin_nested():
            session.execute(
                _INSERT_HEAD,
                {
                    "id": new_id,
                    "t": tenant_id,
                    "m": merchant_id,
                    "b": cart_id,
                    "buyer": buyer_ref,
                    "status": CheckoutState.QUOTED.value,
                    "corr": correlation_id,
                },
            )
    except IntegrityError as exc:
        orig: Any = exc.orig
        if getattr(orig, "sqlstate", None) != _UNIQUE_VIOLATION:
            raise
        constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
        if constraint in (_ONE_CHECKOUT_PER_BASKET, _ONE_CHECKOUT_PER_BASKET_LEGACY):
            raise CheckoutConcurrencyError(
                "checkout_exists_for_basket",
                f"cart {cart_id} already has a checkout; read it rather than create a second",
                code=RecoveryCode.DUPLICATE_OPERATION,
            ) from exc
        raise CheckoutConcurrencyError(
            "checkout_id_taken", f"checkout id {new_id} already exists"
        ) from exc

    session.execute(
        _INSERT_VERSION,
        {
            "id": uuid7(),
            "t": tenant_id,
            "m": merchant_id,
            "c": new_id,
            "v": 1,
            "content": json.dumps(payload, sort_keys=True),
            "h": digest,
            "cur": total.currency,
            "total": total.minor,
            "status": CheckoutState.QUOTED.value,
        },
    )
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=new_id,
        event_type="checkout.version_created",
        actor_type=principal.actor_type if principal is not None else ActorType.BUYER,
        principal_id=principal.principal_id if principal is not None else None,
        payload={
            "version": 1,
            "status": CheckoutState.QUOTED.value,
            "content_hash": digest,
            "total": total,
            "cart_id": cart_id,
            "merchant_id": merchant_id,
            "catalogue_revision": payload["catalogue_revision"],
            "source_id": payload["source_id"],
            "policy_version": payload["policy_version"],
        },
        correlation_id=correlation_id,
    )
    return CheckoutCreated(checkout_id=new_id, version=1, content_hash=digest, total=total)


def supersede_checkout(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    content: Mapping[str, Any],
    correlation_id: uuid.UUID,
    principal: AgentPrincipal | None = None,
) -> CheckoutCreated:
    """Version N+1 of a checkout whose N is spent, in ``QUOTED``.

    Why this exists. One cart has one checkout -- the unique constraint says so -- and a
    buyer who reaches an approval card and then asks for one more item has changed the
    thing being approved. The honest record of that is not a second checkout beside the
    first, which would leave two live documents for one cart, and not an edit to version N,
    which the buyer already saw. It is version N+1: new content, new hash, its own approval,
    with N invalidated behind it. That is the same shape admission already produces when a
    merchant price moves under an approval (:func:`_invalidate_and_supersede`), reached from
    the other direction -- there the merchant moved, here the buyer did.

    The caller ends version N first. This function refuses while N is still live, because a
    checkout whose head points at N+1 while N could still be approved is a checkout with two
    spendable versions, and the buyer would be one press away from paying for the cart they
    just changed.

    Content is re-stamped with this checkout's id and N+1 before hashing, exactly as
    :func:`create_checkout` re-stamps version 1, so the caller prices the cart without
    knowing the version it will become.
    """
    require_context(session, tenant_id)
    _require_correlation(correlation_id)
    if principal is not None and principal.tenant_id != tenant_id:
        raise CheckoutTenantError(
            "principal_tenant_mismatch", "the principal belongs to another tenant"
        )

    head = session.execute(_LOCK_HEAD, {"t": tenant_id, "c": checkout_id}).one_or_none()
    if head is None:
        raise CheckoutStateError("checkout_missing", f"checkout {checkout_id} is not visible")

    previous = int(head.current_version)
    live = session.execute(
        _SELECT_VERSION_STATUS, {"t": tenant_id, "c": checkout_id, "v": previous}
    ).one_or_none()
    if live is None:
        raise CheckoutStateError(
            "version_missing", f"checkout {checkout_id} version {previous} is not visible"
        )
    current_status = CheckoutState(live.status)
    if current_status not in _SUPERSEDABLE_FROM:
        raise CheckoutStateError(
            "version_still_live",
            f"version {previous} is {current_status.value}; end it before superseding",
            code=RecoveryCode.CONCURRENT_OPERATION,
        )

    next_version = previous + 1
    payload = dict(content)
    payload["checkout_id"] = str(checkout_id)
    payload["version"] = next_version
    validate_checkout_content(payload)
    digest = content_hash(payload)
    total = total_of(payload)

    session.execute(
        _INSERT_VERSION,
        {
            "id": uuid7(),
            "t": tenant_id,
            "m": head.merchant_id,
            "c": checkout_id,
            "v": next_version,
            "content": json.dumps(payload, sort_keys=True),
            "h": digest,
            "cur": total.currency,
            "total": total.minor,
            "status": CheckoutState.QUOTED.value,
        },
    )
    sync_head(
        session,
        tenant_id=tenant_id,
        checkout_id=checkout_id,
        version=next_version,
        status=CheckoutState.QUOTED,
    )
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout_id,
        event_type="checkout.version_created",
        actor_type=principal.actor_type if principal is not None else ActorType.BUYER,
        principal_id=principal.principal_id if principal is not None else None,
        payload={
            "version": next_version,
            "status": CheckoutState.QUOTED.value,
            "content_hash": digest,
            "total": total,
            "cart_id": head.cart_id,
            "merchant_id": head.merchant_id,
            "catalogue_revision": payload["catalogue_revision"],
            "source_id": payload["source_id"],
            "policy_version": payload["policy_version"],
            "supersedes": previous,
            "supersedes_status": current_status.value,
        },
        correlation_id=correlation_id,
    )
    return CheckoutCreated(
        checkout_id=checkout_id, version=next_version, content_hash=digest, total=total
    )


# -------------------------------------------------------------------- approval request


def freeze_for_approval(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    receipt: ReceiptInputs,
    correlation_id: uuid.UUID,
    reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    allocations: Sequence[reservations.Allocation] = (),
    principal: AgentPrincipal | None = None,
) -> ApprovalCard:
    """Freeze a version and put it in front of the buyer.

    Two entry states are accepted, and the difference is deliberate:

    * ``QUOTED`` -- the normal path. The machine has no ``QUOTED -> APPROVAL_REQUIRED``
      edge, so the version is moved ``QUOTED -> RESERVED -> APPROVAL_REQUIRED`` in one
      transaction, the hold being taken before the first step so that a version never
      reads RESERVED without a reservation behind it.
    * ``APPROVAL_REQUIRED`` **with no receipt bound** -- the supersede path. When admission
      finds merchant state changed it writes version N+1 already in APPROVAL_REQUIRED
      (ADR D4c) and then calls this to give N+1 its receipt and its fresh reservation
      inside the same admission transaction. No transition is applied on that path; the
      version is already where it must be, and issuing the receipt is the missing half.

    In both cases: the reservation is taken (``allocations`` carries the scarce items and
    the stock the caller re-read; an empty sequence takes a plain hold), the
    Policy-at-Sale Receipt is issued and bound, ``immutable`` is set, the head mirrors
    ``APPROVAL_REQUIRED``, and ``checkout.approval_required`` is audited.

    Refuses: a stored hash that differs from the caller's; an invalidated version; a
    version already awaiting approval with its receipt (``DUPLICATE_OPERATION``); any
    other status (``STALE_CHECKOUT``); a hold the reservation module will not grant
    (:class:`CheckoutReservationError` carrying its code).
    """
    require_context(session, tenant_id)
    _require_correlation(correlation_id)
    if principal is not None and principal.tenant_id != tenant_id:
        raise CheckoutTenantError(
            "principal_tenant_mismatch", "the principal belongs to another tenant"
        )

    locked = lock_version(session, tenant_id=tenant_id, checkout=checkout)
    if locked.invalidated_at is not None:
        raise CheckoutStateError("version_invalidated", "an invalidated version is never revived")

    # A version that prices nothing must never be put in front of a buyer. Reserving an
    # empty sequence skips both loops in ``reservations.reserve`` and so takes a hold
    # against no capacity at all; the version would then be frozen immutable, given a
    # Policy-at-Sale Receipt, and offered as something to approve, with no purchase inside
    # it. Both keys are checked because the two content shapes in this schema name their
    # units differently, and neither is validated here: a caller writing the legacy
    # minimal document is out of scope for this guard, which asks only whether anything
    # is being sold.
    if not (locked.content.get("line_items") or locked.content.get("lines")):
        # A usage error, and so a 500, on purpose. The buyer's own route already refuses an
        # empty cart with a 409 before a version exists, so nothing a buyer can do reaches
        # this line. If it ever fires, a version with nothing in it was written by us, and
        # answering 409 would describe our bug to the buyer as a state they could resolve.
        raise CheckoutUsageError(
            "empty_cart",
            "a version with no priced line cannot be put in front of a buyer",
        )

    path: tuple[CheckoutState, ...]
    if locked.status is CheckoutState.QUOTED:
        path = (CheckoutState.QUOTED, CheckoutState.RESERVED, CheckoutState.APPROVAL_REQUIRED)
    elif locked.status is CheckoutState.APPROVAL_REQUIRED and locked.policy_receipt_id is None:
        path = (CheckoutState.APPROVAL_REQUIRED,)
    elif locked.status is CheckoutState.APPROVAL_REQUIRED:
        raise CheckoutStateError(
            "already_awaiting_approval",
            "this version already carries its receipt and awaits the buyer",
            code=RecoveryCode.DUPLICATE_OPERATION,
        )
    else:
        raise CheckoutStateError(
            "wrong_status",
            f"approval can be requested from QUOTED, not {locked.status.value}",
            current=locked.status,
            target=CheckoutState.APPROVAL_REQUIRED,
        )

    # Every edge on the path is checked before anything is written, so an illegal path
    # fails before a hold has been taken for it.
    for current, target in zip(path, path[1:], strict=False):
        try:
            assert_transition(current, target)
        except InvalidTransitionError as exc:  # pragma: no cover - the path is a constant
            raise CheckoutStateError(
                "illegal_transition", str(exc), current=current, target=target
            ) from exc

    held = reservations.reserve(
        session,
        checkout_id=checkout.checkout_id,
        checkout_version=checkout.version,
        ttl_seconds=reservation_ttl_seconds,
        allocations=allocations,
    )
    if held.reservation is None or held.code not in (
        RecoveryCode.OK,
        RecoveryCode.DUPLICATE_OPERATION,
    ):
        # DUPLICATE_OPERATION means a live hold already exists for this version, which is
        # exactly what the supersede path re-entering here should find; it is not a
        # refusal. Anything else is the reservation module refusing to hold the stock.
        raise CheckoutReservationError(
            "reservation_refused",
            f"no hold could be taken for version {checkout.version}: {held.code.value}",
            code=held.code,
        )

    for current, target in zip(path, path[1:], strict=False):
        apply_transition(
            session, tenant_id=tenant_id, checkout=checkout, current=current, target=target
        )

    issued = receipts.issue_receipt(
        session,
        ReceiptDraft(
            tenant_id=tenant_id,
            merchant_id=locked.merchant_id,
            checkout_id=checkout.checkout_id,
            checkout_version=checkout.version,
            checkout_hash=checkout.content_hash,
            policies=receipt.policies,
            tax_policy_version=receipt.tax_policy_version,
            rounding_policy_version=receipt.rounding_policy_version,
            buyer_visible_refs=receipt.buyer_visible_refs,
            correlation_id=correlation_id,
            store_id=receipt.store_id,
        ),
    )
    session.execute(
        _SET_IMMUTABLE, {"t": tenant_id, "c": checkout.checkout_id, "v": checkout.version}
    )
    head_updated = sync_head(
        session,
        tenant_id=tenant_id,
        checkout_id=checkout.checkout_id,
        version=checkout.version,
        status=CheckoutState.APPROVAL_REQUIRED,
    )
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout.checkout_id,
        event_type="checkout.approval_required",
        actor_type=principal.actor_type if principal is not None else ActorType.SYSTEM,
        principal_id=principal.principal_id if principal is not None else None,
        payload={
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "path": [state.value for state in path],
            "receipt_id": issued.receipt_id,
            "receipt_hash": issued.receipt_hash,
            "total": locked.total,
            "reservation_id": held.reservation.reservation_id,
            "reservation_seconds_remaining": held.reservation.seconds_remaining,
            "reservation_outcome": held.code.value,
            "head_updated": head_updated,
        },
        correlation_id=correlation_id,
    )
    return ApprovalCard(
        checkout=checkout,
        receipt_id=issued.receipt_id,
        receipt_hash=issued.receipt_hash,
        total=locked.total,
        reservation_expires_at=held.reservation.expires_at,
        merchant_id=locked.merchant_id,
        content=locked.content,
    )


# ------------------------------------------------------------------------ transitions


def transition(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    target: CheckoutState,
    reason: str,
    actor: ActorType,
    correlation_id: uuid.UUID,
    principal_id: str | None = None,
) -> TransitionResult:
    """Move one version one legal step, under its lock, with the head and audit in tow.

    This is the general-purpose mutator for the worker and the API (EXECUTION_PENDING,
    AWAITING_PAYMENT, PAID, PAYMENT_FAILED ...). It does not release reservations, revoke
    grants or touch payment attempts: those have their own owners, and a caller that needs
    them combined uses :func:`cancel` or :func:`invalidate_open`.
    """
    require_context(session, tenant_id)
    _require_correlation(correlation_id)
    if not isinstance(target, CheckoutState):
        raise CheckoutUsageError("bad_target", f"target must be a CheckoutState, got {target!r}")
    if not isinstance(reason, str) or not reason:
        raise CheckoutUsageError("bad_reason", "reason must be a non-empty stable key")

    locked = lock_version(session, tenant_id=tenant_id, checkout=checkout)
    apply_transition(
        session, tenant_id=tenant_id, checkout=checkout, current=locked.status, target=target
    )
    head_updated = sync_head(
        session,
        tenant_id=tenant_id,
        checkout_id=checkout.checkout_id,
        version=checkout.version,
        status=target,
    )
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout.checkout_id,
        event_type="checkout.transitioned",
        actor_type=actor,
        principal_id=principal_id,
        payload={
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "from": locked.status.value,
            "to": target.value,
            "reason": reason,
            "head_updated": head_updated,
        },
        correlation_id=correlation_id,
    )
    return TransitionResult(
        checkout=checkout, from_state=locked.status, to_state=target, head_updated=head_updated
    )


def _cancel_denial_code(state: CheckoutState) -> tuple[RecoveryCode, str]:
    """Why the state table refused ``state -> CANCELLED``, as a code and a reason key."""
    if state is CheckoutState.CANCELLED:
        return RecoveryCode.DUPLICATE_OPERATION, "already_cancelled"
    if state is CheckoutState.PAYMENT_UNKNOWN:
        return RecoveryCode.PAYMENT_UNKNOWN, "payment_outcome_unknown"
    if state in (
        CheckoutState.AWAITING_PAYMENT,
        CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT,
    ):
        return RecoveryCode.PAYMENT_PENDING, "payment_surface_open"
    if is_terminal(state):
        return RecoveryCode.STALE_CHECKOUT, "version_terminal"
    return RecoveryCode.POLICY_EXCEPTION, "cancellation_not_representable"  # pragma: no cover


def cancel(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    principal: AgentPrincipal,
    reason: str,
    correlation_id: uuid.UUID,
) -> CancelResult:
    """Cancel one version within policy, or explain why not. Never raises for a refusal.

    Eligibility is the state table's decision, not this function's: every state with a
    ``CANCELLED`` edge (DRAFT through EXECUTION_PENDING, and PAYMENT_FAILED) is cancellable
    here; ``AWAITING_PAYMENT`` and ``PAYMENT_UNKNOWN`` are refused with ``PAYMENT_PENDING``
    and ``PAYMENT_UNKNOWN`` because money may already be moving, and the remedy is
    :func:`invalidate_open` followed by reconciliation (specification 10.8).

    When allowed, in ADR D5 lock order: the version is locked; the reservation is
    released with cause ``CANCELLED``; a ``CREATED`` payment attempt (located earlier with
    a plain SELECT) is locked and moved to ``EXPIRED``; every ``ISSUED`` grant on the
    version is revoked through :mod:`transaction_kernel.grants`; the version becomes
    ``CANCELLED``; the head mirrors it; ``checkout.cancelled`` is audited. A refusal is
    audited as ``checkout.cancel_denied`` with the same care, because a denied
    cancellation is evidence too.

    A live attempt that is past ``CREATED`` while the version still reads cancellable is
    refused before any lock is taken (``PAYMENT_PENDING``, ``attempt_in_flight``): the
    attempt is the more conservative truth, and the worker's version transition is
    serialized behind our lock, so the two can disagree only transiently.
    """
    require_context(session, tenant_id)
    _require_correlation(correlation_id)
    if principal.tenant_id != tenant_id:
        raise CheckoutTenantError(
            "principal_tenant_mismatch", "the principal belongs to another tenant"
        )
    if not isinstance(reason, str) or not reason:
        raise CheckoutUsageError("bad_reason", "reason must be a non-empty stable key")

    def deny(code: RecoveryCode, explanation: str, state: CheckoutState | None) -> CancelResult:
        audit.append(
            session,
            tenant=tenant_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=checkout.checkout_id,
            event_type="checkout.cancel_denied",
            actor_type=principal.actor_type,
            principal_id=principal.principal_id,
            payload={
                "version": checkout.version,
                "content_hash": checkout.content_hash,
                "code": code.value,
                "explanation": explanation,
                "reason": reason,
                "state": None if state is None else state.value,
            },
            correlation_id=correlation_id,
        )
        return CancelResult(
            allowed=False, code=code, explanation=explanation, checkout=checkout, from_state=state
        )

    # ADR D5: locate the attempt with a plain SELECT before any lock is taken.
    live_attempt = _find_live_attempt(session, tenant_id, checkout)

    locked = _lock_row(session, tenant_id, checkout)
    if locked is None:
        return deny(RecoveryCode.STALE_CHECKOUT, "checkout_version_not_found", None)
    if locked.content_hash != checkout.content_hash:
        return deny(
            RecoveryCode.STALE_CHECKOUT, "approved_hash_does_not_match_stored", locked.status
        )
    if not can_transition(locked.status, CheckoutState.CANCELLED):
        code, explanation = _cancel_denial_code(locked.status)
        return deny(code, explanation, locked.status)
    if live_attempt is not None and live_attempt[1] is not PaymentState.CREATED:
        return deny(RecoveryCode.PAYMENT_PENDING, "attempt_in_flight", locked.status)

    release = reservations.release(
        session,
        checkout_id=checkout.checkout_id,
        checkout_version=checkout.version,
        cause=reservations.ReleaseCause.CANCELLED,
    )

    attempt_expired: uuid.UUID | None = None
    if live_attempt is not None:
        attempt_id, _ = live_attempt
        row = session.execute(_LOCK_ATTEMPT, {"t": tenant_id, "id": attempt_id}).one_or_none()
        if row is None or PaymentState(row.status) is not PaymentState.CREATED:
            raise CheckoutConcurrencyError(
                "attempt_changed_under_version_lock",
                f"payment attempt {attempt_id} moved from CREATED while this transaction "
                "held the version lock; a writer bypassed ADR D5",
            )
        assert_transition(PaymentState.CREATED, PaymentState.EXPIRED)
        expired = cast(
            "CursorResult[Any]",
            session.execute(
                _EXPIRE_ATTEMPT,
                {
                    "t": tenant_id,
                    "id": attempt_id,
                    "expired": PaymentState.EXPIRED.value,
                    "created": PaymentState.CREATED.value,
                },
            ),
        )
        if expired.rowcount != 1:  # pragma: no cover - the row is locked above
            raise CheckoutConcurrencyError(
                "attempt_changed_under_lock", f"payment attempt {attempt_id} was not expired"
            )
        attempt_expired = attempt_id

    revoked: list[uuid.UUID] = []
    grant_rows = session.execute(
        _FIND_ISSUED_GRANTS,
        {"t": tenant_id, "c": checkout.checkout_id, "v": checkout.version},
    ).all()
    for grant_row in grant_rows:
        try:
            grants.revoke_grant(session, grant_row.id)
        except grants.GrantAlreadyConsumedError as exc:
            # The worker consumed the grant without moving the version first. Refuse to
            # half-cancel: the provider request may be in flight.
            raise CheckoutConcurrencyError(
                "grant_consumed_under_version_lock",
                f"grant {grant_row.id} was consumed while the version still read "
                f"{locked.status.value}",
            ) from exc
        revoked.append(grant_row.id)

    apply_transition(
        session,
        tenant_id=tenant_id,
        checkout=checkout,
        current=locked.status,
        target=CheckoutState.CANCELLED,
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
        event_type="checkout.cancelled",
        actor_type=principal.actor_type,
        principal_id=principal.principal_id,
        payload={
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "from": locked.status.value,
            "reason": reason,
            "reservation_release": release.code.value,
            "attempt_expired": attempt_expired,
            "grants_revoked": revoked,
            "head_updated": head_updated,
        },
        correlation_id=correlation_id,
    )
    return CancelResult(
        allowed=True,
        code=RecoveryCode.OK,
        explanation="cancelled",
        checkout=checkout,
        from_state=locked.status,
        grants_revoked=tuple(revoked),
        attempt_expired=attempt_expired,
        reservation_release=release.code,
    )


def invalidate_open(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout: CheckoutRef,
    reason: str,
    correlation_id: uuid.UUID,
    actor: ActorType = ActorType.SYSTEM,
    principal_id: str | None = None,
) -> TransitionResult:
    """Merchant state moved while the payment surface was open, specification 10.8.

    The version goes to ``INVALIDATED_AWAITING_PAYMENT_RESULT`` with ``invalidated_at``
    stamped; the state table permits this from ``AWAITING_PAYMENT`` and from
    ``PAYMENT_UNKNOWN``, and refuses it from everywhere else. The reservation is kept on
    purpose: a capture may still arrive, and stock that was in fact paid for must not be
    resold before the late capture is refunded. The payment attempt is not touched here;
    reconciliation owns it from this point.
    """
    require_context(session, tenant_id)
    _require_correlation(correlation_id)
    if not isinstance(reason, str) or not reason:
        raise CheckoutUsageError("bad_reason", "reason must be a non-empty stable key")

    locked = lock_version(session, tenant_id=tenant_id, checkout=checkout)
    target = CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT
    apply_transition(
        session, tenant_id=tenant_id, checkout=checkout, current=locked.status, target=target
    )
    head_updated = sync_head(
        session,
        tenant_id=tenant_id,
        checkout_id=checkout.checkout_id,
        version=checkout.version,
        status=target,
    )
    audit.append(
        session,
        tenant=tenant_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=checkout.checkout_id,
        event_type="checkout.invalidated_open",
        actor_type=actor,
        principal_id=principal_id,
        payload={
            "version": checkout.version,
            "content_hash": checkout.content_hash,
            "from": locked.status.value,
            "to": target.value,
            "reason": reason,
            "reservation_held": True,
            "head_updated": head_updated,
        },
        correlation_id=correlation_id,
    )
    return TransitionResult(
        checkout=checkout, from_state=locked.status, to_state=target, head_updated=head_updated
    )


# ------------------------------------------------------------------------------ reads


def read_head(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> CheckoutHead | None:
    """The head row, or ``None`` when this tenant has no such checkout."""
    require_context(session, tenant_id)
    row = session.execute(_SELECT_HEAD, {"t": tenant_id, "c": checkout_id}).one_or_none()
    if row is None:
        return None
    return CheckoutHead(
        checkout_id=row.id,
        tenant_id=row.tenant_id,
        merchant_id=row.merchant_id,
        cart_id=row.cart_id,
        buyer_ref=str(row.buyer_ref),
        current_version=int(row.current_version),
        status=CheckoutState(row.status),
        correlation_id=row.correlation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def read_versions(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> tuple[CheckoutVersionView, ...]:
    """Every version of one checkout, oldest first. Empty when none is visible."""
    require_context(session, tenant_id)
    rows = session.execute(_SELECT_VERSIONS, {"t": tenant_id, "c": checkout_id}).all()
    return tuple(_view(row) for row in rows)


def current_version(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> CheckoutVersionView | None:
    """The highest-numbered version, which is the one a buyer can still act on."""
    require_context(session, tenant_id)
    row = session.execute(_SELECT_CURRENT_VERSION, {"t": tenant_id, "c": checkout_id}).one_or_none()
    return None if row is None else _view(row)
