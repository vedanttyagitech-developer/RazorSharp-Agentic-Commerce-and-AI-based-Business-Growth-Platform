"""Hash-chained append-only audit, specification 26.2.

Why this module exists
----------------------
Evidence that can be edited is not evidence. ``audit_events`` has no UPDATE and no DELETE
grant for any application role, which stops the application from rewriting history. It
does not stop someone with database-owner credentials, a restored backup, or a replica
with different contents. So the rows carry their own integrity: each event hashes its own
content *and its predecessor's hash*, which means a single edited field, a removed row or
a swapped pair breaks every link after it, and no amount of privilege re-forges the chain
without rewriting the entire stream from that point to the head.

What is in the hash, and why
----------------------------
``self_hash`` is :func:`commerce_domain.canonical_hash` over the envelope built by
:func:`event_envelope`. Every column that carries meaning is inside it:

* ``prev_hash`` -- the link. Without it each row is independently verifiable and the
  *stream* is not: rows could be deleted, reordered or spliced freely.
* ``seq`` -- position. Without it two events could exchange places while both still
  verified individually.
* ``tenant_id``, ``aggregate_type``, ``aggregate_id`` -- the stream identity. Without
  them a valid event could be relocated wholesale into another tenant's stream.
* ``event_type``, ``actor_type``, ``actor_id``, ``principal_id``, ``payload`` -- the
  claim itself and who made it. Attribution is half of what an audit is for.
* ``correlation_id`` -- the thread a dispute is reconstructed along.
* ``occurred_at_ms`` -- when. A back-dated event is a forged alibi, so the timestamp is
  chained like everything else.

The envelope is versioned by ``ENVELOPE_SCHEMA``. Stored hashes must stay reproducible
for as long as the evidence is retained, so a new field means a new schema tag and a
verifier that knows both -- never a silent change of meaning under the old tag.

What this design does NOT detect
--------------------------------
Truncation of the *tail*. Deleting the newest events leaves a stream that is internally
consistent and verifies clean; there is nothing left in it that expects the deleted rows
to exist. This is a property of every self-contained hash chain, and pretending otherwise
would be worse than naming it. Detection requires an anchor outside the stream:

* the aggregate's own state row, which reached a status that only a recorded event could
  have produced; and
* a periodically published head -- :func:`head` returns ``(seq, self_hash)`` -- written
  somewhere the same operator cannot silently rewrite.

Both are outside this module. :func:`verify_chain` reports the head it verified so a
caller can compare it against such an anchor.

Same transaction as the state change (specification 26.2)
---------------------------------------------------------
:func:`append` takes the caller's :class:`~sqlalchemy.orm.Session` and never commits,
never rolls back, and never opens a transaction of its own. The audit row therefore
commits with the state change it describes or vanishes with it. There is no code path
here that can leave a payment recorded with no evidence, or evidence for a payment that
never happened.

The same rule runs in the failing direction: when an event cannot be appended this module
*raises* rather than returning a code. A caller that ignored a returned code could commit
a money movement whose evidence was refused, so the refusal is made impossible to ignore.

Sequence allocation and contention
----------------------------------
``seq`` is per ``(tenant, aggregate_type, aggregate_id)`` and starts at 1, gapless. Two
appends to the same stream are serialized on a transaction-scoped advisory lock taken
before the tail is read, so the loser blocks until the winner commits and then reads the
winner's row as its predecessor. This is not merely an optimisation: ``prev_hash`` has to
be the *committed* tail, so the read and the insert must be atomic with respect to each
other.

The unique constraint on ``(tenant_id, aggregate_type, aggregate_id, seq)`` is the
backstop underneath that lock. If it ever fires, the serialization assumption was wrong
and this module raises :class:`AuditConcurrencyError` instead of quietly retrying: a
silent retry would paper over the one condition that could otherwise produce two events
claiming the same position in one stream.

Requires READ COMMITTED, PostgreSQL's default. Under a snapshot-stable isolation level
the tail read can be blind to a concurrently committed event; the unique constraint then
refuses the insert, which is the fail-closed direction.

Nothing here calls a model, and nothing here consults an application clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from commerce_domain import DomainError, Money, canonical_hash, uuid7
from platform_db import AuditEvent, require_tenant
from sqlalchemy import Row, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .contracts import ActorType
from .recovery import RecoveryCode

__all__ = [
    "ENVELOPE_SCHEMA",
    "FIRST_SEQ",
    "AuditConcurrencyError",
    "AuditContentError",
    "AuditError",
    "AuditEventView",
    "AuditTenantError",
    "AuditUsageError",
    "BreakKind",
    "ChainBreak",
    "ChainVerification",
    "StreamHead",
    "append",
    "compute_self_hash",
    "envelope_of",
    "event_envelope",
    "head",
    "read_stream",
    "verify_chain",
]

#: Version tag inside every hashed envelope. A stored hash must stay reproducible for the
#: life of the evidence, so a change to the hashed shape gets a new tag and a verifier
#: that understands both, never a redefinition of this one.
ENVELOPE_SCHEMA: Final = "audit_event/1"

#: Every stream starts here. ``audit_events`` also carries ``CHECK (seq >= 1)``.
FIRST_SEQ: Final = 1

# Column widths from ``platform_db.schema.AuditEvent``. Checked in Python so an over-long
# value is refused by name, rather than arriving as an opaque driver error that has
# already aborted the caller's transaction -- and with it the state change being audited.
MAX_AGGREGATE_TYPE_LENGTH: Final = 48
MAX_EVENT_TYPE_LENGTH: Final = 64
MAX_ACTOR_TYPE_LENGTH: Final = 32
MAX_ACTOR_ID_LENGTH: Final = 128
MAX_PRINCIPAL_ID_LENGTH: Final = 128

#: PostgreSQL ``unique_violation``. Only this SQLSTATE means "another transaction already
#: took this seq". A foreign-key or row-level-security failure is also an
#: ``IntegrityError`` and must never be reported as a sequence race.
_UNIQUE_VIOLATION: Final = "23505"

#: Epoch anchor for the millisecond timestamp inside the envelope. Reconstructing the
#: stored ``occurred_at`` as ``_EPOCH + timedelta(milliseconds=n)`` is exact integer
#: arithmetic in both directions, so the value that was hashed and the value in the row
#: can never drift apart by a rounding step.
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------- errors


class AuditError(DomainError):
    """An audit event could not be appended, or a stream could not be trusted."""


class AuditUsageError(AuditError):
    """The caller's session cannot carry an audit write (no transaction bound)."""


class AuditTenantError(AuditError):
    """The event names a tenant other than the one bound to this transaction."""


class AuditContentError(AuditError):
    """The event is not a complete, canonicalizable, reproducible record."""


class AuditConcurrencyError(AuditError):
    """Another transaction took this ``seq`` first.

    Carries :attr:`code` from the closed recovery enum so a caller that forwards the
    failure does not have to invent one. The caller's transaction must not commit: the
    state change it was recording has no evidence.
    """

    code: Final = RecoveryCode.CONCURRENT_OPERATION


# ----------------------------------------------------------------------------- values


class BreakKind(StrEnum):
    """The first thing wrong with a stream. This is the machine-readable verdict.

    Kept distinct rather than collapsed into "tampered" because each one names a
    different act, and an investigator's next step differs: a gap points at a delete, a
    link mismatch at a reorder or splice, a content mismatch at an in-place edit.
    """

    SEQ_NOT_FIRST = "SEQ_NOT_FIRST"
    """The stream does not begin at 1: its opening events were removed."""

    SEQ_GAP = "SEQ_GAP"
    """A sequence number is missing: a row in the middle was deleted."""

    GENESIS_PREV_HASH = "GENESIS_PREV_HASH"
    """Event 1 carries a ``prev_hash``: the stream was re-rooted onto a fabricated
    history that no longer exists to contradict it.

    Reserved for position 1. A *later* event whose ``prev_hash`` is null is a severed
    link rather than a forged root, and is reported as ``PREV_HASH_MISMATCH`` with
    ``found`` null -- the investigator's next step is the same either way, and
    collapsing the two would make ``at_seq`` the only thing distinguishing them."""

    PREV_HASH_MISMATCH = "PREV_HASH_MISMATCH"
    """An event does not chain to the event before it: rows were reordered, swapped or
    substituted, even if each one hashes correctly on its own."""

    SELF_HASH_MISMATCH = "SELF_HASH_MISMATCH"
    """An event's stored hash does not match its stored content: it was edited in place,
    or its stored hash was."""


@dataclass(frozen=True, slots=True)
class AuditEventView:
    """One audit event exactly as the database holds it.

    ``occurred_at_ms`` is derived from the stored ``occurred_at`` and is what the
    envelope hashes; it is carried here so verification never has to re-derive it from a
    local clock or a display format.
    """

    event_id: uuid.UUID
    tenant_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    seq: int
    event_type: str
    actor_type: str
    actor_id: str | None
    principal_id: str | None
    payload: Mapping[str, Any]
    prev_hash: str | None
    self_hash: str
    correlation_id: uuid.UUID
    occurred_at: datetime
    occurred_at_ms: int


@dataclass(frozen=True, slots=True)
class StreamHead:
    """The newest event in a stream: its position and its hash.

    Publishing this somewhere the database operator cannot silently rewrite is what turns
    tail truncation from undetectable into detectable. See the module docstring.
    """

    seq: int
    self_hash: str


@dataclass(frozen=True, slots=True)
class ChainBreak:
    """The first broken link, and enough to point an investigator at the act.

    ``expected`` and ``found`` are rendered as strings because what they hold differs by
    kind: a sequence number for a gap, a hash for a link or content failure.

    ``detail`` is an operator-facing sentence for the verifier CLI of specification 26.2.
    ``kind`` is the contract; nothing may branch on ``detail``.
    """

    kind: BreakKind
    at_seq: int
    event_id: uuid.UUID | None
    expected: str | None
    found: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The structured result of walking one aggregate's stream.

    ``head_seq`` and ``head_hash`` describe the last event that *verified*, not the last
    row present. On an intact stream those are the same thing; on a broken one the head
    is the last position still worth anchoring against.
    """

    aggregate_type: str
    aggregate_id: uuid.UUID
    length: int
    events_verified: int
    head_seq: int | None
    head_hash: str | None
    first_break: ChainBreak | None

    @property
    def intact(self) -> bool:
        """True when every link from 1 to the head verified."""
        return self.first_break is None

    @property
    def empty(self) -> bool:
        """True when the aggregate has no events. Vacuously intact, and rarely benign:
        an aggregate that reached a financial state with no events is itself a finding."""
        return self.length == 0

    @property
    def code(self) -> RecoveryCode:
        """The closed-enum verdict.

        A broken chain is never an automatic retry and never a buyer-facing message: it
        is an integrity failure that a person has to look at, so it maps to
        ``HUMAN_REVIEW_REQUIRED``.
        """
        return RecoveryCode.OK if self.intact else RecoveryCode.HUMAN_REVIEW_REQUIRED


# -------------------------------------------------------------------------------- SQL

# Every SQL string below is composed from literals in this module. No value from a
# request, a model or a database row is ever joined into SQL text; everything variable
# travels as a bound parameter.

# now() is the transaction timestamp, so all events appended for one state change carry
# the same instant -- correct, because they describe one atomic change. Their order
# within the stream is carried by seq, which is the authority; the clock never is.
# Truncated to milliseconds so the value that goes into the hash and the value stored in
# the timestamptz column are the same number, with no sub-millisecond remainder to lose.
_DB_NOW_MS = text("SELECT (EXTRACT(EPOCH FROM date_trunc('milliseconds', now())) * 1000)::bigint")

# Transaction-scoped: released at COMMIT or ROLLBACK, so a crashed pod cannot strand it.
# Keyed on tenant + aggregate because advisory locks are cluster-global and are not
# filtered by row-level security. A hashtextextended collision costs false contention
# between unrelated streams, never a missed exclusion within one stream.
_ADVISORY_XACT_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))")

_STREAM_SCOPE: Final = (
    " FROM audit_events"
    " WHERE tenant_id = :tenant_id"
    "   AND aggregate_type = :aggregate_type"
    "   AND aggregate_id = :aggregate_id"
)

# tenant_id is repeated in the predicate even though row-level security already applies
# it: it makes the unique index on (tenant_id, aggregate_type, aggregate_id, seq) usable,
# and it means a stream read is still scoped if this ever runs under a role or a table
# where RLS is not in force.
_SELECT_TAIL = text("SELECT seq, self_hash" + _STREAM_SCOPE + " ORDER BY seq DESC LIMIT 1")

_SELECT_STREAM = text(
    "SELECT id, aggregate_type, aggregate_id, seq, event_type, actor_type, actor_id,"
    "       principal_id, payload, prev_hash, self_hash, correlation_id, occurred_at"
    + _STREAM_SCOPE
    + " ORDER BY seq"
)


# --------------------------------------------------------------------------- envelope


def _canonical_payload(value: object, path: str) -> Any:
    """Convert a payload value into the integer-only JSON profile, or refuse.

    The refusals are not fussiness. Every one of these types either cannot be
    canonicalized at all, or round-trips through JSONB as a *different* value, which
    would make the stored event fail its own hash check the next time it is verified --
    an honest record indistinguishable from a tampered one.
    """
    if isinstance(value, Money):
        # Expanded rather than stringified so a later reader compares integers instead of
        # re-parsing "395.00" and guessing the exponent.
        return {"currency": value.currency, "minor": value.minor}
    if isinstance(value, uuid.UUID):
        return str(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        # Normalises str subclasses (StrEnum members in particular) to plain str, so the
        # hashed bytes do not depend on which enum class produced the label.
        return str(value)
    if isinstance(value, float | Decimal):
        raise AuditContentError(
            f"non-integer number at {path}: audit payloads are integers only. Money is "
            "commerce_domain.Money (integer minor units) and rates are integer basis "
            "points. A float cannot be canonicalized, so an event holding one could "
            "never be re-verified."
        )
    if isinstance(value, datetime):
        raise AuditContentError(
            f"datetime at {path}: record an integer epoch instead. A datetime's "
            "canonical form depends on tzinfo and formatting, so the event hash would "
            "not be reproducible by a verifier that formatted it differently."
        )
    if isinstance(value, bytes | bytearray):
        raise AuditContentError(
            f"raw bytes at {path}: audit payloads hold references and hashes, never "
            "protocol material. Record a hash or a key reference."
        )
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditContentError(
                    f"non-string payload key {key!r} at {path}: JSON object keys must be "
                    "strings, and a non-string key would be silently coerced by JSONB"
                )
            out[str(key)] = _canonical_payload(item, f"{path}.{key}")
        return out
    # str and bytes are Sequences too; both are settled above, so anything reaching here
    # is a genuine list or tuple.
    if isinstance(value, Sequence):
        return [_canonical_payload(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise AuditContentError(
        f"cannot record {type(value).__name__} at {path} in an audit payload; sets and "
        "arbitrary objects have no stable JSON form"
    )


def event_envelope(
    *,
    tenant_id: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    seq: int,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    principal_id: str | None,
    payload: Mapping[str, Any],
    prev_hash: str | None,
    correlation_id: uuid.UUID,
    occurred_at_ms: int,
) -> dict[str, Any]:
    """The exact structure whose canonical hash is ``self_hash``.

    Public because specification 26.2 requires an independent verifier: a CLI, an
    external auditor or a future re-implementation must be able to reproduce the hash
    without reading this module's private internals.

    Guarantees a value that :func:`commerce_domain.canonical_hash` accepts, given a
    payload that already passed :func:`_canonical_payload`. Key order is irrelevant --
    JCS sorts -- but every key is always present, including the ones whose value is
    ``None``, so an absent field and a null field can never hash alike.
    """
    return {
        "schema": ENVELOPE_SCHEMA,
        "tenant_id": str(tenant_id),
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id),
        "seq": seq,
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "principal_id": principal_id,
        "payload": dict(payload),
        "prev_hash": prev_hash,
        "correlation_id": str(correlation_id),
        "occurred_at_ms": occurred_at_ms,
    }


def compute_self_hash(envelope: Mapping[str, Any]) -> str:
    """Hash one envelope. The single definition of ``audit_events.self_hash``.

    Named rather than inlined so a verifier never has to know *which* hash function this
    platform uses, only that this is the one that produced the stored value.
    """
    digest: str = canonical_hash(dict(envelope))
    return digest


def envelope_of(event: AuditEventView) -> dict[str, Any]:
    """Rebuild the hashed envelope from a stored event.

    This is the function that makes tampering detectable: it reads only what the row
    actually contains, so an edited column produces a different envelope and therefore a
    different hash from the one stored beside it.
    """
    return event_envelope(
        tenant_id=event.tenant_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        seq=event.seq,
        event_type=event.event_type,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        principal_id=event.principal_id,
        payload=event.payload,
        prev_hash=event.prev_hash,
        correlation_id=event.correlation_id,
        occurred_at_ms=event.occurred_at_ms,
    )


# -------------------------------------------------------------------------- internals


def _require_transaction(session: Session) -> None:
    """Refuse a session that is not already inside a transaction.

    Specification 26.2 requires the audit row to commit with the state change it
    describes. A session with no open transaction would begin one here, and the evidence
    would then live in a unit of work of its own -- able to commit while the state change
    rolled back, or the reverse.
    """
    if not session.in_transaction():
        raise AuditUsageError(
            "append requires an active transaction: the audit row must commit with the "
            "state change it describes. Open the caller's transaction first (see "
            "platform_db.session_scope) and pass that session."
        )


def _check_tenant(session: Session, tenant: uuid.UUID) -> uuid.UUID:
    if not isinstance(tenant, uuid.UUID):
        raise AuditContentError(f"tenant must be a UUID, got {type(tenant).__name__}")
    bound: uuid.UUID = require_tenant(session)
    if bound != tenant:
        # Row-level security would reject the INSERT with a policy violation whose
        # message names neither tenant. Failing here keeps a cross-tenant write attempt
        # legible, which is itself the kind of thing an audit trail exists to record.
        raise AuditTenantError(
            f"event names tenant {tenant} but this transaction is bound to {bound}; "
            "an audit event is written into the stream of its own tenant or not at all"
        )
    return bound


def _text_field(value: str, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise AuditContentError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise AuditContentError(
            f"{field} is {len(value)} characters; the column holds {limit}. Truncation "
            "would change the hashed envelope, so the value is refused instead."
        )
    return value


def _optional_text(value: str | None, *, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _text_field(value, field=field, limit=limit)


def _actor_type(value: ActorType | str) -> str:
    """Normalise to a known actor label. An unknown one never enters the evidence."""
    if isinstance(value, ActorType):
        return str(value.value)
    try:
        return str(ActorType(value).value)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in ActorType))
        raise AuditContentError(
            f"actor_type {value!r} is not a known ActorType. Attribution is half of what "
            f"an audit record is for, so an unrecognised actor is refused. Known: {known}"
        ) from exc


def _lock_stream(
    session: Session, tenant_id: uuid.UUID, aggregate_type: str, aggregate_id: uuid.UUID
) -> None:
    session.execute(
        _ADVISORY_XACT_LOCK,
        {"lock_key": f"audit:{tenant_id}:{aggregate_type}:{aggregate_id}"},
    )


def _tail(
    session: Session, tenant_id: uuid.UUID, aggregate_type: str, aggregate_id: uuid.UUID
) -> StreamHead | None:
    row = session.execute(
        _SELECT_TAIL,
        {
            "tenant_id": tenant_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        },
    ).one_or_none()
    return None if row is None else StreamHead(seq=int(row.seq), self_hash=str(row.self_hash))


def _view(row: Row[Any], tenant_id: uuid.UUID) -> AuditEventView:
    """Materialise one stored row, deriving the millisecond stamp that was hashed.

    Floor division of exact ``timedelta`` values, not a float conversion: a row whose
    ``occurred_at`` carries sub-millisecond precision (which this module never writes)
    keeps that remainder out of the derived value, so its recomputed hash will not match
    and the row is reported as altered. That is the fail-closed direction.
    """
    occurred_at: datetime = row.occurred_at
    return AuditEventView(
        event_id=row.id,
        tenant_id=tenant_id,
        aggregate_type=str(row.aggregate_type),
        aggregate_id=row.aggregate_id,
        seq=int(row.seq),
        event_type=str(row.event_type),
        actor_type=str(row.actor_type),
        actor_id=None if row.actor_id is None else str(row.actor_id),
        principal_id=None if row.principal_id is None else str(row.principal_id),
        payload=row.payload,
        prev_hash=None if row.prev_hash is None else str(row.prev_hash),
        self_hash=str(row.self_hash),
        correlation_id=row.correlation_id,
        occurred_at=occurred_at,
        occurred_at_ms=(occurred_at - _EPOCH) // timedelta(milliseconds=1),
    )


def _database_now_ms(session: Session) -> int:
    """Transaction time from the database clock, in epoch milliseconds.

    There is no local-clock fallback. A pod skewed by a minute must not be able to stamp
    financial evidence with a time the database never saw.
    """
    return int(session.execute(_DB_NOW_MS).scalar_one())


# ----------------------------------------------------------------------------- public


def append(
    session: Session,
    *,
    tenant: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    actor_type: ActorType | str,
    principal_id: str | None,
    payload: Mapping[str, Any],
    correlation_id: uuid.UUID,
    actor_id: str | None = None,
) -> AuditEventView:
    """Append one event to an aggregate's hash chain, inside the caller's transaction.

    Guarantees:

    * The row is written on ``session`` and nothing here commits, rolls back or opens a
      transaction. The event commits with the state change it describes or vanishes with
      it -- specification 26.2's first requirement, and the reason this takes a session
      rather than a connection factory.
    * ``seq`` is the next integer for ``(tenant, aggregate_type, aggregate_id)``,
      starting at :data:`FIRST_SEQ`, gapless and strictly increasing. Concurrent appends
      to one stream are serialized on an advisory lock held for the rest of the
      transaction, so the loser chains onto the winner's committed row.
    * ``prev_hash`` is the committed tail's ``self_hash``, and ``NULL`` only for
      ``seq = 1``. ``self_hash`` is :func:`compute_self_hash` over
      :func:`event_envelope`, which covers every meaningful column including the
      timestamp -- so editing any of them, deleting a row or exchanging two rows breaks
      the chain from that point to the head.
    * ``occurred_at`` is the database transaction clock, truncated to milliseconds so the
      stored value and the hashed value are the same number.

    Refuses, by raising -- never by returning a code a caller could ignore and then
    commit money movement without evidence:

    * :class:`AuditUsageError` when the session has no open transaction.
    * :class:`AuditTenantError` when ``tenant`` is not the tenant bound to it.
    * :class:`AuditContentError` for a payload that cannot be canonicalized or would not
      survive a JSONB round trip (float, Decimal, datetime, bytes, non-string keys), for
      an unknown ``actor_type``, and for a field wider than its column.
    * :class:`AuditConcurrencyError` when the unique constraint on
      ``(tenant_id, aggregate_type, aggregate_id, seq)`` refuses the insert. The caller's
      transaction must not commit.

    Returns the event as stored, including its ``seq``, ``prev_hash`` and ``self_hash``.
    """
    _require_transaction(session)
    tenant_id = _check_tenant(session, tenant)

    if not isinstance(aggregate_id, uuid.UUID):
        raise AuditContentError(f"aggregate_id must be a UUID, got {type(aggregate_id).__name__}")
    if not isinstance(correlation_id, uuid.UUID):
        # NOT NULL in the schema, and the only thread along which a dispute is
        # reconstructed across services. An event without one is unusable evidence.
        raise AuditContentError(
            f"correlation_id must be a UUID, got {type(correlation_id).__name__}"
        )
    if not isinstance(payload, Mapping):
        raise AuditContentError(
            f"payload must be a mapping, got {type(payload).__name__}; an audit event "
            "records named fields so a later reader does not have to guess positions"
        )

    aggregate_type = _text_field(
        aggregate_type, field="aggregate_type", limit=MAX_AGGREGATE_TYPE_LENGTH
    )
    event_type = _text_field(event_type, field="event_type", limit=MAX_EVENT_TYPE_LENGTH)
    actor = _text_field(_actor_type(actor_type), field="actor_type", limit=MAX_ACTOR_TYPE_LENGTH)
    actor_id = _optional_text(actor_id, field="actor_id", limit=MAX_ACTOR_ID_LENGTH)
    principal_id = _optional_text(principal_id, field="principal_id", limit=MAX_PRINCIPAL_ID_LENGTH)
    body: dict[str, Any] = _canonical_payload(dict(payload), "$payload")

    # Taken before the tail is read and held until the caller's transaction ends. The
    # read and the insert have to be atomic with respect to each other: prev_hash must be
    # the committed tail, not the tail as it stood a moment ago.
    _lock_stream(session, tenant_id, aggregate_type, aggregate_id)

    tail = _tail(session, tenant_id, aggregate_type, aggregate_id)
    seq = FIRST_SEQ if tail is None else tail.seq + 1
    prev_hash = None if tail is None else tail.self_hash

    occurred_at_ms = _database_now_ms(session)
    occurred_at = _EPOCH + timedelta(milliseconds=occurred_at_ms)

    envelope = event_envelope(
        tenant_id=tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        seq=seq,
        event_type=event_type,
        actor_type=actor,
        actor_id=actor_id,
        principal_id=principal_id,
        payload=body,
        prev_hash=prev_hash,
        correlation_id=correlation_id,
        occurred_at_ms=occurred_at_ms,
    )
    self_hash = compute_self_hash(envelope)
    event_id = uuid7()

    try:
        # A SAVEPOINT so that a unique violation does not poison the caller's whole
        # transaction before this module can report which stream it happened on.
        with session.begin_nested():
            session.execute(
                insert(AuditEvent).values(
                    id=event_id,
                    tenant_id=tenant_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    seq=seq,
                    event_type=event_type,
                    actor_type=actor,
                    actor_id=actor_id,
                    principal_id=principal_id,
                    payload=body,
                    prev_hash=prev_hash,
                    self_hash=self_hash,
                    correlation_id=correlation_id,
                    occurred_at=occurred_at,
                )
            )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != _UNIQUE_VIOLATION:
            # A foreign-key or row-level-security failure is not a sequence race and must
            # not be reported as one.
            raise
        raise AuditConcurrencyError(
            f"seq {seq} was already taken on {aggregate_type} {aggregate_id}; the "
            "advisory lock did not serialize this append (a non-default isolation level "
            "will do that). The state change this event describes must not commit."
        ) from exc

    return AuditEventView(
        event_id=event_id,
        tenant_id=tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        seq=seq,
        event_type=event_type,
        actor_type=actor,
        actor_id=actor_id,
        principal_id=principal_id,
        payload=body,
        prev_hash=prev_hash,
        self_hash=self_hash,
        correlation_id=correlation_id,
        occurred_at=occurred_at,
        occurred_at_ms=occurred_at_ms,
    )


def read_stream(
    session: Session,
    *,
    tenant: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
) -> tuple[AuditEventView, ...]:
    """Every event for one aggregate, in ``seq`` order.

    Ordered by ``seq`` rather than by ``occurred_at``: several events of one atomic state
    change share a transaction timestamp, and a clock is not what orders evidence here.

    Raises :class:`AuditTenantError` if ``tenant`` is not the bound tenant, rather than
    returning the empty result row-level security would otherwise produce -- silence
    would read as "this aggregate has no history".

    Reads the whole stream. Deliberate, and bounded by the aggregates this kernel
    audits: a checkout or a payment attempt accumulates tens of events, not millions. A
    chain cannot be verified from a page anyway -- the walk has to start at ``seq`` 1 --
    so an aggregate that ever grows unbounded needs a checkpointing scheme here, not a
    LIMIT.
    """
    tenant_id = _check_tenant(session, tenant)
    rows = session.execute(
        _SELECT_STREAM,
        {
            "tenant_id": tenant_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        },
    ).all()
    return tuple(_view(row, tenant_id) for row in rows)


def head(
    session: Session,
    *,
    tenant: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
) -> StreamHead | None:
    """The newest event's position and hash, or ``None`` for an empty stream.

    Cheap on purpose: this is the value an operator publishes to an external anchor so
    that truncating the tail -- the one edit a self-contained chain cannot detect --
    becomes detectable by comparison.
    """
    tenant_id = _check_tenant(session, tenant)
    return _tail(session, tenant_id, aggregate_type, aggregate_id)


def verify_chain(
    session: Session,
    *,
    tenant: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
) -> ChainVerification:
    """Walk one aggregate's stream and name the first broken link.

    This is the verifier of specification 26.2. It recomputes every hash from the stored
    columns; it never trusts a stored hash to describe the row it sits on.

    Checks, per event, in this order:

    1. ``seq`` is the next expected position -- ``SEQ_NOT_FIRST`` if the stream does not
       open at 1, ``SEQ_GAP`` if a number is missing.
    2. The link -- ``GENESIS_PREV_HASH`` if event 1 carries a ``prev_hash``;
       ``PREV_HASH_MISMATCH`` if a later event does not name its predecessor's hash,
       including when it names nothing at all.
    3. The content -- ``SELF_HASH_MISMATCH`` if the stored hash is not the hash of the
       stored columns.

    Structure is checked before content deliberately. When a row has been spliced in, its
    content mismatch is a consequence of the splice; reporting the missing or misplaced
    row first points at the act rather than at its symptom.

    Returns a :class:`ChainVerification` naming the FIRST break only. Everything after a
    break is unverifiable rather than merely suspect: a genuine event that follows a
    deleted one cannot chain to a row that is gone, so listing later positions as
    findings would manufacture noise around a single act.

    Honest limit: a stream whose newest events were deleted verifies clean. Compare
    ``head_hash`` against an anchor published outside the database to detect that.

    Raises :class:`AuditTenantError` if ``tenant`` is not the tenant bound to this
    transaction.
    """
    events = read_stream(
        session, tenant=tenant, aggregate_type=aggregate_type, aggregate_id=aggregate_id
    )

    expected_seq = FIRST_SEQ
    previous: AuditEventView | None = None
    verified = 0
    fault: ChainBreak | None = None

    for event in events:
        if event.seq != expected_seq:
            kind = BreakKind.SEQ_NOT_FIRST if previous is None else BreakKind.SEQ_GAP
            detail = (
                f"stream opens at seq {event.seq}; events 1..{event.seq - 1} are missing"
                if previous is None
                else f"seq {expected_seq} is missing between {expected_seq - 1} and {event.seq}"
            )
            fault = ChainBreak(
                kind=kind,
                at_seq=expected_seq,
                event_id=event.event_id,
                expected=str(expected_seq),
                found=str(event.seq),
                detail=detail,
            )
            break

        if previous is None:
            if event.prev_hash is not None:
                fault = ChainBreak(
                    kind=BreakKind.GENESIS_PREV_HASH,
                    at_seq=event.seq,
                    event_id=event.event_id,
                    expected=None,
                    found=event.prev_hash,
                    detail=(
                        "the first event of a stream has no predecessor, so its "
                        "prev_hash must be null; this one names one"
                    ),
                )
                break
        elif event.prev_hash != previous.self_hash:
            fault = ChainBreak(
                kind=BreakKind.PREV_HASH_MISMATCH,
                at_seq=event.seq,
                event_id=event.event_id,
                expected=previous.self_hash,
                found=event.prev_hash,
                detail=(
                    f"seq {event.seq} does not chain to seq {previous.seq}: the events "
                    "were reordered, swapped or spliced, even though each hashes "
                    "correctly on its own"
                ),
            )
            break

        recomputed = compute_self_hash(envelope_of(event))
        if recomputed != event.self_hash:
            fault = ChainBreak(
                kind=BreakKind.SELF_HASH_MISMATCH,
                at_seq=event.seq,
                event_id=event.event_id,
                expected=recomputed,
                found=event.self_hash,
                detail=(
                    f"seq {event.seq} does not hash to its stored self_hash: a column "
                    "was edited in place, or the stored hash was"
                ),
            )
            break

        verified += 1
        previous = event
        expected_seq += 1

    return ChainVerification(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        length=len(events),
        events_verified=verified,
        head_seq=None if previous is None else previous.seq,
        head_hash=None if previous is None else previous.self_hash,
        first_break=fault,
    )
