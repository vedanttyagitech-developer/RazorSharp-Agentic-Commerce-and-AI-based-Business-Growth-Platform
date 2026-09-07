"""Idempotency records, specification 10.6.

The failure this module exists to prevent: an agent, a mobile client or a retrying HTTP
proxy sends the same business operation twice. Without a record, the second delivery
creates a second Razorpay order, a second refund or a second reserve debit. The buyer is
charged twice and nothing in the system knows it happened.

Three outcomes, and only three:

===============================  ===========================  ==========================
Situation                        Behaviour                    ``RecoveryCode``
===============================  ===========================  ==========================
Key unused                       execute exactly once         ``OK``
Key used, identical request      return the stored response,  ``DUPLICATE_OPERATION``
                                 execute nothing
Key used, different request      refuse; execute nothing;     ``POLICY_EXCEPTION``
                                 disclose nothing
===============================  ===========================  ==========================

The third row is the one that matters most. A retry whose amount changed from 395.00 to
3950.00 but whose ``Idempotency-Key`` did not is either a client bug or an attack. Both
answers that feel natural are wrong: re-executing charges the buyer the new amount under
a key that promised not to re-execute, and returning the stored response tells the caller
"3950.00 succeeded" when what actually happened was 395.00. So the request is refused,
and the refusal carries ``POLICY_EXCEPTION`` rather than ``DUPLICATE_OPERATION``:
``DUPLICATE_OPERATION`` is deliberately excluded from ``recovery.NOT_A_SUCCESS``, meaning
a caller is entitled to present it to a buyer as a completed money action. Nothing
completed here, so that code would be a lie with a receipt attached.

Atomicity contract
------------------
The record is claimed and the response is stored inside the *caller's* transaction. The
idempotency record therefore commits with the effect it describes, and never without it:

* caller's transaction commits  -> the record exists and the effect happened;
* caller's transaction rolls back -> neither exists, and a retry may proceed.

There is no window in which a committed record claims an operation that never ran.
A second caller racing on the same key blocks on the unique index
``uq_idempotency_records_tenant_id_idem_key`` until the first transaction ends, then
either observes the winner's stored response or, if the winner rolled back, claims the
key itself. Single-winner is a database guarantee here, not a hopeful code path.

Isolation level
---------------
This module requires ``READ COMMITTED`` (PostgreSQL's default). After losing the race on
the unique index it re-reads the row, which only returns the winner's row if each
statement takes a fresh snapshot. Under ``REPEATABLE READ`` the loser would see neither
its own failed insert nor the winner's committed row; that state is detected and raised
as a usage error rather than silently retried.

Clocks
------
Nothing here reads an application clock. ``created_at`` is filled by the database's
``now()`` default, so a pod with a skewed clock cannot backdate or postdate a record.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, NoReturn, cast

from commerce_domain import DomainError, canonical_hash, canonicalize, uuid7
from platform_db import IdempotencyRecord, require_tenant
from sqlalchemy import CursorResult, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .contracts import Operation
from .recovery import RecoveryCode

__all__ = [
    "MAX_KEY_LENGTH",
    "MAX_OPERATION_LENGTH",
    "IdempotencyError",
    "IdempotencyInFlightError",
    "IdempotencyKeyReuseError",
    "IdempotencyRecordView",
    "IdempotencySlot",
    "IdempotencyUsageError",
    "IdempotentOutcome",
    "IdempotentReplayError",
    "execute_once",
    "idempotent",
    "read_idempotency_record",
]

#: ``idempotency_records.idem_key`` is ``VARCHAR(128)``. Checked here so an over-long key
#: is refused by name rather than surfacing as an opaque driver error mid-transaction.
MAX_KEY_LENGTH: Final = 128

#: ``idempotency_records.operation`` is ``VARCHAR(48)``.
MAX_OPERATION_LENGTH: Final = 48

#: PostgreSQL ``unique_violation``. Only this SQLSTATE means "someone else claimed the
#: key". A foreign-key or row-level-security failure is also an ``IntegrityError`` and
#: must never be reported as a duplicate.
_UNIQUE_VIOLATION: Final = "23505"


# --------------------------------------------------------------------------- signals


class IdempotencyError(DomainError):
    """Base for every outcome this module raises instead of executing.

    Each instance carries a :class:`RecoveryCode` from the closed enum. Callers that do
    not care which of the three it was may catch this class and forward ``.code``.
    """

    code: RecoveryCode

    def __init__(self, message: str, key: str, operation: str) -> None:
        super().__init__(message)
        self.key = key
        self.operation = operation


class IdempotentReplayError(IdempotencyError):
    """The key was already used for this exact request, and the result is known.

    Raised from ``idempotent()``'s ``__enter__`` so that the guarded block cannot run.
    A boolean flag on a yielded object would be forgettable; an exception is not.
    """

    code = RecoveryCode.DUPLICATE_OPERATION

    def __init__(self, key: str, operation: str, response: dict[str, Any]) -> None:
        super().__init__(
            f"idempotency key {key!r} already completed operation {operation}; "
            "returning the original response without re-executing",
            key,
            operation,
        )
        self.response = response


class IdempotencyKeyReuseError(IdempotencyError):
    """The key was already used for a *different* request. Nothing is executed.

    Carries no response: handing back the original result would answer a question the
    caller did not ask, and a caller that logged it as its own outcome would record the
    wrong amount against the wrong request.
    """

    code = RecoveryCode.POLICY_EXCEPTION

    def __init__(
        self,
        key: str,
        operation: str,
        *,
        stored_operation: str,
        stored_request_hash: str,
        offered_request_hash: str,
    ) -> None:
        super().__init__(
            f"idempotency key {key!r} is bound to operation {stored_operation} with "
            f"request hash {stored_request_hash}; this request is operation {operation} "
            f"with request hash {offered_request_hash}. An idempotency key binds to "
            "exactly one request; reuse with different content is refused, never re-run.",
            key,
            operation,
        )
        self.stored_operation = stored_operation
        self.stored_request_hash = stored_request_hash
        self.offered_request_hash = offered_request_hash


class IdempotencyInFlightError(IdempotencyError):
    """The key is claimed by a committed record that carries no response yet.

    Under this module's own atomicity contract this cannot arise from a clean run: the
    claim and the response commit together. It means a writer committed a claim without
    a result -- a crash between two commits, or a caller that swallowed an exception and
    committed anyway. The operation may or may not have reached the provider, so the
    answer is the same as any unknown outcome (specification 10.7): read current state
    and reconcile. Do not execute, and do not retry blindly.
    """

    code = RecoveryCode.CONCURRENT_OPERATION

    def __init__(self, key: str, operation: str, record_id: uuid.UUID) -> None:
        super().__init__(
            f"idempotency key {key!r} is claimed by record {record_id} which has no "
            "stored response; the outcome is unknown and must be reconciled, not re-run",
            key,
            operation,
        )
        self.record_id = record_id


class IdempotencyUsageError(DomainError):
    """The caller used this module incorrectly.

    Deliberately not an :class:`IdempotencyError`: there is no recovery code because
    there is no recovery. It is a bug to be fixed, not a state to be explained to a
    buyer.
    """


# ------------------------------------------------------------------------ read model


@dataclass(frozen=True, slots=True)
class IdempotencyRecordView:
    """Read-only projection of one ``idempotency_records`` row.

    ``response is None`` means claimed but not completed. A completed operation always
    stores a JSON object, possibly empty -- see :meth:`IdempotencySlot.store`.
    """

    record_id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    operation: str
    request_hash: str
    response: dict[str, Any] | None
    created_at: datetime

    @property
    def is_complete(self) -> bool:
        return self.response is not None


@dataclass(frozen=True, slots=True)
class IdempotentOutcome:
    """What :func:`execute_once` returns.

    ``executed`` is the honest fact the caller needs for audit: whether *this* call is
    the one that moved money, or whether it is reading back the call that did.
    """

    response: dict[str, Any]
    code: RecoveryCode
    executed: bool


# ----------------------------------------------------------------------- validation


def _normalise_operation(operation: Operation | str) -> str:
    """Coerce to the stored string form, refusing values the column cannot hold."""
    text = str(operation)
    if not text:
        raise IdempotencyUsageError("operation must be a non-empty string")
    if len(text) > MAX_OPERATION_LENGTH:
        raise IdempotencyUsageError(
            f"operation is {len(text)} characters; the column holds {MAX_OPERATION_LENGTH}"
        )
    return text


def _validate_key(key: str) -> str:
    """Refuse keys the column cannot hold, or that carry no identity.

    An over-long key silently truncated to 128 characters would make two *different*
    operations share a record, which is the exact collision this table prevents.
    """
    if not isinstance(key, str):
        raise IdempotencyUsageError(f"idempotency key must be str, got {type(key).__name__}")
    if not key.strip():
        raise IdempotencyUsageError("idempotency key must not be empty or blank")
    if len(key) > MAX_KEY_LENGTH:
        raise IdempotencyUsageError(
            f"idempotency key is {len(key)} characters; the column holds {MAX_KEY_LENGTH}. "
            "Truncation would make two different operations share one record."
        )
    return key


def _require_transaction(session: Session) -> None:
    if not session.in_transaction():
        raise IdempotencyUsageError(
            "idempotency requires an active transaction: the record must commit with "
            "the effect it describes, never before it and never without it"
        )


# ---------------------------------------------------------------------- persistence


def _load(session: Session, tenant_id: uuid.UUID, key: str) -> IdempotencyRecordView | None:
    """Read the record for ``key`` in the bound tenant, or None.

    The tenant predicate is written explicitly even though row-level security already
    filters the query. Defence in depth: if this statement is ever executed by a role
    that is exempt from RLS, the tenant scoping still holds.
    """
    row = session.execute(
        select(
            IdempotencyRecord.id,
            IdempotencyRecord.tenant_id,
            IdempotencyRecord.idem_key,
            IdempotencyRecord.operation,
            IdempotencyRecord.request_hash,
            IdempotencyRecord.response,
            IdempotencyRecord.created_at,
        ).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.idem_key == key,
        )
    ).one_or_none()
    if row is None:
        return None
    return IdempotencyRecordView(
        record_id=row.id,
        tenant_id=row.tenant_id,
        key=row.idem_key,
        operation=row.operation,
        request_hash=row.request_hash,
        response=row.response,
        created_at=row.created_at,
    )


def _refuse(existing: IdempotencyRecordView, operation: str, request_hash: str) -> NoReturn:
    """Turn an existing record into the one correct refusal. Always raises.

    ``NoReturn`` is load-bearing, not decoration: it makes the type checker prove that
    no path through :func:`_claim` can fall past a refusal and hand back a slot for a
    key someone else already owns.

    Order matters. The content check runs first and unconditionally: if the request
    differs, the caller learns nothing about the stored result, not even whether the
    original completed.
    """
    if existing.operation != operation or existing.request_hash != request_hash:
        raise IdempotencyKeyReuseError(
            existing.key,
            operation,
            stored_operation=existing.operation,
            stored_request_hash=existing.request_hash,
            offered_request_hash=request_hash,
        )
    if existing.response is None:
        raise IdempotencyInFlightError(existing.key, operation, existing.record_id)
    raise IdempotentReplayError(existing.key, operation, existing.response)


def _claim(
    session: Session,
    tenant_id: uuid.UUID,
    key: str,
    operation: str,
    request_hash: str,
) -> uuid.UUID:
    """Insert the claim row, or raise the refusal the existing row demands.

    Two callers reaching the insert at the same instant is the case this is written for.
    The second one blocks on the unique index until the first transaction ends, so the
    winner is decided by PostgreSQL rather than by whichever process was scheduled first.
    The losing insert is wrapped in a SAVEPOINT: without it the unique violation would
    abort the caller's whole transaction, and the caller could not even read back the
    winner's response.
    """
    existing = _load(session, tenant_id, key)
    if existing is not None:
        _refuse(existing, operation, request_hash)

    record_id: uuid.UUID = uuid7()
    try:
        with session.begin_nested():
            session.execute(
                insert(IdempotencyRecord).values(
                    id=record_id,
                    tenant_id=tenant_id,
                    idem_key=key,
                    operation=operation,
                    request_hash=request_hash,
                    # Left NULL on purpose: the row is a claim until store() completes it.
                    response=None,
                    # created_at is omitted so the database clock fills it. An application
                    # clock must not be able to stamp financial evidence.
                )
            )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != _UNIQUE_VIOLATION:
            # A foreign-key or RLS failure is not a duplicate and must not be reported
            # as one; a mis-scoped tenant would otherwise look like a successful replay.
            raise
        winner = _load(session, tenant_id, key)
        if winner is None:
            raise IdempotencyUsageError(
                f"idempotency key {key!r} collided on the unique index but no record is "
                "visible. This module requires READ COMMITTED isolation; under a "
                "snapshot-stable level the winning row cannot be read back."
            ) from exc
        _refuse(winner, operation, request_hash)
    return record_id


# ----------------------------------------------------------------------------- slot


class IdempotencySlot:
    """A claimed, not-yet-completed idempotency record.

    Yielded by :func:`idempotent` only when this caller won the key. Holding one is the
    permission to execute exactly once; :meth:`store` is how the result is bound to it.
    """

    __slots__ = (
        "_session",
        "_stored",
        "key",
        "operation",
        "record_id",
        "request_hash",
        "tenant_id",
    )

    def __init__(
        self,
        session: Session,
        *,
        record_id: uuid.UUID,
        tenant_id: uuid.UUID,
        key: str,
        operation: str,
        request_hash: str,
    ) -> None:
        self._session = session
        self._stored = False
        self.record_id = record_id
        self.tenant_id = tenant_id
        self.key = key
        self.operation = operation
        self.request_hash = request_hash

    @property
    def stored(self) -> bool:
        """Whether a response has been bound to this slot."""
        return self._stored

    def store(self, response: Mapping[str, Any]) -> None:
        """Bind the operation's result to this record. Callable exactly once.

        Guarantees the stored bytes are reproducible: ``response`` is canonicalized
        under the platform's RFC 8785 integer-only profile before it is written, so a
        float that escaped :class:`commerce_domain.Money` is refused here rather than
        frozen into a record that every future replay hands back.

        Refuses a second call, a non-mapping, and a response that does not land on
        exactly this record. ``{}`` is the correct value for an operation with no
        payload: a ``NULL`` response means "claimed but unfinished", and writing one
        deliberately would wedge the key for good.
        """
        if self._stored:
            raise IdempotencyUsageError(
                f"response already stored for idempotency key {self.key!r}; a slot "
                "authorises exactly one result"
            )
        if not isinstance(response, Mapping):
            raise IdempotencyUsageError(
                f"response must be a JSON object mapping, got {type(response).__name__}. "
                "Use {} for an operation with no payload."
            )
        payload = dict(response)
        # Raises CanonicalizationError on float, non-string keys, or any type that has
        # no stable JSON form. Better a loud failure now than an unverifiable replay.
        canonicalize(payload)

        # The tenant predicate is redundant under row-level security and kept anyway:
        # a slot must only ever complete the one record it claimed.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.id == self.record_id,
                    IdempotencyRecord.tenant_id == self.tenant_id,
                )
                .values(response=payload)
            ),
        )
        if result.rowcount != 1:
            raise IdempotencyUsageError(
                f"expected to complete exactly one idempotency record, updated "
                f"{result.rowcount}; record {self.record_id} is missing or not visible "
                "to the bound tenant"
            )
        self._stored = True


# ------------------------------------------------------------------------ public API


@contextmanager
def idempotent(
    session: Session,
    key: str,
    operation: Operation | str,
    request: Any,
) -> Iterator[IdempotencySlot]:
    """Guard a business operation so it runs at most once per (tenant, key).

    Usage::

        with idempotent(session, key, Operation.PAYMENT_CREATE_ORDER, request) as slot:
            response = create_the_order()
            slot.store(response)

    Guarantees:

    * the guarded block runs only when this caller won the key -- on a replay the block
      is never entered, because ``IdempotentReplayError`` is raised before it;
    * the claim, the stored response and everything the block wrote commit together, or
      not at all: a block that raises leaves no claim behind, so the key stays usable;
    * ``request`` is compared by ``commerce_domain.canonical_hash``, so JSON key order
      and whitespace do not matter but a changed amount does.

    Refuses, without executing anything:

    * :class:`IdempotentReplayError` -- same key, same request, result known
      (``DUPLICATE_OPERATION``);
    * :class:`IdempotencyKeyReuseError` -- same key, different request (``POLICY_EXCEPTION``);
    * :class:`IdempotencyInFlightError` -- key claimed, outcome unknown
      (``CONCURRENT_OPERATION``);
    * :class:`IdempotencyUsageError` -- no transaction, no tenant bound, an unusable key,
      or a block that exits without calling :meth:`IdempotencySlot.store`.

    The tenant is taken from the tenant bound to this transaction, never from an
    argument, so a record can never be written under a tenant that disagrees with the
    row-level-security context that will later read it.
    """
    _require_transaction(session)
    tenant_id = require_tenant(session)
    key = _validate_key(key)
    op = _normalise_operation(operation)
    request_hash = canonical_hash(request)

    # A savepoint spanning the claim and the guarded block. If the block raises, the
    # claim is rolled back with it: a caller that catches the error and commits anyway
    # must not leave a committed claim for an operation that never happened, because
    # every later retry of that key would then be refused as an unknown outcome.
    outer = session.begin_nested()
    try:
        record_id = _claim(session, tenant_id, key, op, request_hash)
        slot = IdempotencySlot(
            session,
            record_id=record_id,
            tenant_id=tenant_id,
            key=key,
            operation=op,
            request_hash=request_hash,
        )
        yield slot
        if not slot.stored:
            raise IdempotencyUsageError(
                f"idempotency slot for key {key!r} was never given a response. "
                "Committing it would claim an operation whose result is unknown; "
                "call slot.store(...) -- use {} when there is no payload."
            )
    except BaseException:
        outer.rollback()
        raise
    outer.commit()


def execute_once(
    session: Session,
    key: str,
    operation: Operation | str,
    request: Any,
    run: Callable[[], Mapping[str, Any]],
) -> IdempotentOutcome:
    """Run ``run`` at most once for (tenant, key), returning the original result after.

    The ergonomic form of :func:`idempotent` for the common case. ``run`` is invoked
    exactly once on the first successful use and never again for that key; a duplicate
    call returns the stored response with ``DUPLICATE_OPERATION`` and does not touch
    ``run``.

    Refusals are *not* folded into the return value. :class:`IdempotencyKeyReuseError` and
    :class:`IdempotencyInFlightError` propagate, because a returned outcome can be ignored
    while an exception cannot, and both of those mean "this request did not happen".
    Catch :class:`IdempotencyError` and forward ``.code`` to handle them uniformly.
    """
    try:
        with idempotent(session, key, operation, request) as slot:
            response = run()
            slot.store(response)
            stored = dict(response)
    except IdempotentReplayError as replay:
        return IdempotentOutcome(
            response=replay.response, code=RecoveryCode.DUPLICATE_OPERATION, executed=False
        )
    return IdempotentOutcome(response=stored, code=RecoveryCode.OK, executed=True)


def read_idempotency_record(session: Session, key: str) -> IdempotencyRecordView | None:
    """Read the record for ``key`` in the bound tenant without claiming anything.

    For reconciliation (specification 10.7), where the question is "did this operation
    ever get a result?" rather than "may I run it?". Returns None when the key is unused
    in this tenant; a record whose ``response`` is None was claimed but never completed.
    """
    _require_transaction(session)
    tenant_id = require_tenant(session)
    return _load(session, tenant_id, _validate_key(key))
