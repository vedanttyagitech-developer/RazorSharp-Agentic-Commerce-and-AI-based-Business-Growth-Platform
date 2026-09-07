"""Delegated-payment Safe Mode, specification 10.3.2.

Safe Mode is the platform's kill switch for *machine-initiated* money movement. It is the
control that lets an operator stop new delegated debits during a key compromise, a
provider incident or a reconciliation backlog without taking the platform down and
without stranding the buyers who are already mid-transaction.

The distinction that makes it safe
----------------------------------
A kill switch that stops everything is not a kill switch, it is an outage, and an outage
harms the buyer it was meant to protect: their refund does not land, their reconciliation
does not finish, and the support agent looking at their order sees nothing. So Safe Mode
is deliberately asymmetric.

*Stopped* (:data:`SAFE_MODE_BLOCKED`): new Reserve Pay and delegated-authority debits,
new delegated Execution Grants at admission, new delegated authorities, and external
autonomous completion. Everything here is a machine acting on a buyer's money without the
buyer present -- exactly the class of action an incident makes untrustworthy.

*Kept available* (:data:`SAFE_MODE_PERMITTED`): fresh human-present Razorpay Standard
Checkout, refunds, reconciliation, order tracking, support actions, revoking a delegated
authority, appending audit evidence, and every read. A human is present or the buyer is
being made whole; neither becomes more dangerous because a delegated path is compromised.

Three things Safe Mode deliberately does *not* do
-------------------------------------------------
1. It does not convert ``UNKNOWN`` payment outcomes to ``FAILED``. An incident is not
   evidence that a buyer's money stayed put. Unknown outcomes stay unknown and stay
   reconcilable; :data:`GuardedOperation.RECONCILIATION` remains permitted precisely so
   they can be resolved while the switch is on.
2. It does not erase evidence. Consumed Execution Grants keep their ``CONSUMED`` status
   and ``consumed_at``; payment attempts are untouched; mode history is append-only.
   Nothing in this module issues a ``DELETE`` or rewrites a prior record.
3. It does not authenticate anybody. :func:`enter_safe_mode` and :func:`leave_safe_mode`
   take the actor as a parameter and record it. Proving that the actor is who they claim
   to be belongs to the admission layer above this one. The one structural rule enforced
   here is specification 10.3.2's "the LLM cannot enter or leave Safe Mode":
   :data:`~transaction_kernel.contracts.ActorType.AGENT` is refused outright.

Enforcement point
-----------------
:func:`is_permitted` is the enforcement point, and it re-reads the mode from PostgreSQL
inside the caller's transaction every time. Grant revocation on activation is *cleanup*,
not enforcement: a grant the sweep has not reached is still refused, because admission
asks :func:`is_permitted` before it consumes anything. Treating the sweep as the
enforcement point would mean a global activation left every unswept tenant chargeable
until a background job caught up.

Scope precedence
----------------
Modes are recorded globally (``tenant_id IS NULL``) or per tenant, as append-only
history; the current mode of a scope is its newest row. When both scopes have a record,
the rule is asymmetric on purpose and is implemented in :func:`_resolve`:

* The tenant's own record decides, so a tenant may enter Safe Mode for its own incident
  while the platform is ``NORMAL``, and may be explicitly exempted from a platform
  incident by a later per-tenant ``NORMAL``.
* Except: a *global* ``SAFE_MODE`` declared strictly later than the tenant's last record
  covers that tenant anyway. A platform-wide kill switch that a stale tenant row from
  last month could veto is not a kill switch.
* A global ``NORMAL`` never lifts a tenant's own Safe Mode. De-escalation does not
  propagate downward; a tenant leaves Safe Mode only by its own audited action.

Within one scope the newest record wins, and a tie on ``changed_at`` -- which happens
whenever two records of a scope are written in the same transaction, since ``now()`` is
the transaction timestamp -- resolves to ``SAFE_MODE``. Insert order is genuinely not
recoverable there: ``uuid7`` ids carry only a millisecond timestamp plus random bits, so
they sort in random order inside a millisecond. The tie is therefore broken by safety
rather than by a false claim about ordering; see the comment above
``_NEWEST_PER_SCOPE``. A stand-down must be its own transaction to take effect.

Empty history is ``NORMAL``: a platform that has never declared an incident is not in
one, and failing closed on an empty table would refuse every delegated payment on a
freshly migrated database.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from commerce_domain import uuid7
from platform_db import OperatingMode, current_tenant, require_tenant
from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from .contracts import ActorType, Operation
from .grants import revoke_unused_grants
from .recovery import RecoveryCode

__all__ = [
    "DELEGATED_GRANT_OPERATIONS",
    "ENTRY_REASONS",
    "EXIT_REASONS",
    "NEVER_SWEPT",
    "SAFE_MODE_BLOCKED",
    "SAFE_MODE_PERMITTED",
    "GuardedOperation",
    "ModeChangeReason",
    "ModeRecord",
    "ModeResolution",
    "ModeScope",
    "OperatingModeName",
    "SafeModeActivation",
    "SafeModeBanner",
    "SafeModeBlockedError",
    "SafeModeError",
    "SafeModeScopeError",
    "assert_permitted",
    "banner",
    "current_mode",
    "enter_safe_mode",
    "is_permitted",
    "leave_safe_mode",
    "resolve_mode",
]


# --------------------------------------------------------------------------- vocabulary


class OperatingModeName(StrEnum):
    """The two operating modes. Mirrors the ``mode_enum`` check constraint on
    ``platform_operating_modes``; a value not listed here cannot be stored."""

    NORMAL = "NORMAL"
    SAFE_MODE = "SAFE_MODE"


class ModeScope(StrEnum):
    """Which record decided the effective mode."""

    GLOBAL = "GLOBAL"
    TENANT = "TENANT"


class ModeChangeReason(StrEnum):
    """Why the mode changed. A closed enum, never a free-text sentence.

    Specification 10.3.2 allows Safe Mode to be entered by an authorized operator or by
    an allowlisted deterministic incident rule. Both write a member of this enum, so an
    operator console, a metric and an audit query all group the same incident the same
    way. A free-text reason would make "provider outage", "Provider Outage" and "razorpay
    down again" three different incidents in every report that ever counts them.
    """

    # --- entry: why delegated money movement stopped ----------------------
    OPERATOR_DECLARED_INCIDENT = "OPERATOR_DECLARED_INCIDENT"
    KEY_COMPROMISE_EVIDENCE = "KEY_COMPROMISE_EVIDENCE"
    ABNORMAL_DUPLICATE_ATTEMPTS = "ABNORMAL_DUPLICATE_ATTEMPTS"
    UNRESOLVED_RECONCILIATION_BACKLOG = "UNRESOLVED_RECONCILIATION_BACKLOG"
    SIGNATURE_VERIFICATION_ANOMALY = "SIGNATURE_VERIFICATION_ANOMALY"
    PROVIDER_INCIDENT_DECLARED = "PROVIDER_INCIDENT_DECLARED"

    # --- exit: why it is safe to resume -----------------------------------
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    OPERATOR_STOOD_DOWN = "OPERATOR_STOOD_DOWN"
    TENANT_EXEMPTED = "TENANT_EXEMPTED"


#: Reasons that may accompany an entry into Safe Mode.
ENTRY_REASONS: Final[frozenset[ModeChangeReason]] = frozenset(
    {
        ModeChangeReason.OPERATOR_DECLARED_INCIDENT,
        ModeChangeReason.KEY_COMPROMISE_EVIDENCE,
        ModeChangeReason.ABNORMAL_DUPLICATE_ATTEMPTS,
        ModeChangeReason.UNRESOLVED_RECONCILIATION_BACKLOG,
        ModeChangeReason.SIGNATURE_VERIFICATION_ANOMALY,
        ModeChangeReason.PROVIDER_INCIDENT_DECLARED,
    }
)

#: Reasons that may accompany a return to NORMAL. Disjoint from :data:`ENTRY_REASONS` so
#: that the history cannot claim a platform resumed delegated payments *because of*
#: key-compromise evidence -- a record that reads as an incident causing a stand-down is
#: worse than no record, because a reviewer will believe it.
EXIT_REASONS: Final[frozenset[ModeChangeReason]] = frozenset(
    {
        ModeChangeReason.INCIDENT_RESOLVED,
        ModeChangeReason.OPERATOR_STOOD_DOWN,
        ModeChangeReason.TENANT_EXEMPTED,
    }
)


class GuardedOperation(StrEnum):
    """Every operation Safe Mode has an opinion about.

    Closed on purpose, and classified explicitly by :data:`SAFE_MODE_BLOCKED` and
    :data:`SAFE_MODE_PERMITTED`. A member added here without being classified is blocked
    while Safe Mode is active, because :func:`is_permitted` consults the allowlist rather
    than the blocklist: forgetting to classify a new *money-moving* path must not leave a
    hole in the kill switch. ``test_classification_partitions_the_enum`` fails on any
    unclassified member so the mistake is caught at review rather than at 3am.
    """

    # --- machine-initiated money movement; stopped by SAFE_MODE -----------
    DELEGATED_DEBIT = "DELEGATED_DEBIT"
    DELEGATED_GRANT_ISSUE = "DELEGATED_GRANT_ISSUE"
    DELEGATED_AUTHORITY_CREATE = "DELEGATED_AUTHORITY_CREATE"
    AUTONOMOUS_COMPLETION = "AUTONOMOUS_COMPLETION"

    # --- human-present or buyer-protective; kept available by SAFE_MODE ---
    HUMAN_PRESENT_CHECKOUT = "HUMAN_PRESENT_CHECKOUT"
    REFUND_EXECUTE = "REFUND_EXECUTE"
    RECONCILIATION = "RECONCILIATION"
    ORDER_TRACKING = "ORDER_TRACKING"
    SUPPORT_ACTION = "SUPPORT_ACTION"
    AUTHORITY_REVOKE = "AUTHORITY_REVOKE"
    AUDIT_APPEND = "AUDIT_APPEND"
    READ = "READ"


#: Stopped while Safe Mode is active. Every member is a machine acting on a buyer's money
#: in the buyer's absence, which is the class of action an incident makes untrustworthy.
#: ``DELEGATED_AUTHORITY_CREATE`` is here although minting a mandate is not itself a
#: debit: a mandate created during a key-compromise incident is a debit with a delay.
SAFE_MODE_BLOCKED: Final[frozenset[GuardedOperation]] = frozenset(
    {
        GuardedOperation.DELEGATED_DEBIT,
        GuardedOperation.DELEGATED_GRANT_ISSUE,
        GuardedOperation.DELEGATED_AUTHORITY_CREATE,
        GuardedOperation.AUTONOMOUS_COMPLETION,
    }
)

#: Kept available while Safe Mode is active, specification 10.3.2. Each of these either
#: has a human present or exists to make a buyer whole. ``AUDIT_APPEND`` is on the list
#: because a switch that stopped evidence being written would destroy the record of the
#: very incident it was thrown for, and ``AUTHORITY_REVOKE`` because cancelling a
#: delegated authority must never be harder during an incident than outside one.
SAFE_MODE_PERMITTED: Final[frozenset[GuardedOperation]] = frozenset(
    {
        GuardedOperation.HUMAN_PRESENT_CHECKOUT,
        GuardedOperation.REFUND_EXECUTE,
        GuardedOperation.RECONCILIATION,
        GuardedOperation.ORDER_TRACKING,
        GuardedOperation.SUPPORT_ACTION,
        GuardedOperation.AUTHORITY_REVOKE,
        GuardedOperation.AUDIT_APPEND,
        GuardedOperation.READ,
    }
)

#: Grant operations the activation sweep withdraws, specification 10.3.2 "invalidates
#: unused delegated Execution Grants".
#:
#: ``RESERVE_DEBIT`` is the delegated debit and is unambiguously in scope.
#:
#: ``REFUND_EXECUTE`` is deliberately excluded: revoking a live refund grant would cancel
#: a payment the buyer is owed, which is the precise harm this design refuses.
#:
#: ``PAYMENT_CREATE_ORDER`` is also excluded, and this one is a real limitation rather
#: than a preference. The ``execution_grants`` row does not record whether the admission
#: that produced it was delegated or human-present, so a sweep covering it would revoke
#: the order-creation grant of a buyer sitting in front of Razorpay Standard Checkout --
#: the exact path Safe Mode promises to keep open. The delegated case is stopped instead
#: at admission by :func:`is_permitted`, which is the enforcement point in any case. If a
#: presence marker is ever added to ``execution_grants``, this set should widen to cover
#: delegated order creation and the sweep becomes exact.
DELEGATED_GRANT_OPERATIONS: Final[frozenset[Operation]] = frozenset({Operation.RESERVE_DEBIT})

#: Operations the activation sweep may never withdraw, whatever the caller asks for.
#:
#: Excluding ``REFUND_EXECUTE`` from the *default* sweep is not enough on its own, because
#: :func:`enter_safe_mode` takes a ``revoke_operations`` override: without this set, one
#: call passing ``{Operation.REFUND_EXECUTE}`` would cancel refunds the buyer is already
#: owed, which is the precise harm the asymmetry of Safe Mode exists to refuse. The
#: guarantee "a refund already admitted still completes" has to hold against the API, not
#: only against the default argument.
NEVER_SWEPT: Final[frozenset[Operation]] = frozenset({Operation.REFUND_EXECUTE})


# --------------------------------------------------------------------------- failures


class SafeModeError(Exception):
    """A deterministic refusal from the Safe Mode service.

    ``code`` is the structured outcome the caller must act on. Callers translate the code
    into language; they may not override it or substitute a reason of their own.
    """

    code: RecoveryCode = RecoveryCode.POLICY_EXCEPTION


class SafeModeBlockedError(SafeModeError):
    """Safe Mode is active and this operation is one of the ones it stops.

    ``SAFE_MODE_ACTIVE`` is not retryable and is not a payment failure: nothing was sent
    to the provider, and the operation becomes possible again only when an operator
    returns the scope to ``NORMAL``. Presenting it to a buyer as a declined payment would
    be false.
    """

    code = RecoveryCode.SAFE_MODE_ACTIVE

    def __init__(self, message: str, *, resolution: ModeResolution) -> None:
        super().__init__(message)
        #: The full resolution, so the audit record shows which scope and which record
        #: stopped the operation rather than only that something did.
        self.resolution = resolution


class SafeModeScopeError(SafeModeError):
    """The mode change does not match the tenant bound to this transaction.

    ``platform_operating_modes`` carries a nullable tenant and is therefore outside
    row-level security, so this check is the only thing standing between a tenant-scoped
    request and the platform-wide switch. It is a caller error, not a buyer-visible
    outcome, and it is raised rather than returned so it cannot be swallowed as a denial.
    """

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class ModeRecord:
    """One row of the append-only mode history.

    ``reason_code`` is typed ``str`` rather than :class:`ModeChangeReason` on purpose: a
    row written by an older release must still be readable. A mode lookup that raised on
    an unrecognized historical reason would turn a legible incident record into an
    outage.
    """

    record_id: uuid.UUID
    tenant_id: uuid.UUID | None
    mode: OperatingModeName
    reason_code: str
    actor: str
    changed_at: datetime

    @property
    def scope(self) -> ModeScope:
        return ModeScope.GLOBAL if self.tenant_id is None else ModeScope.TENANT


@dataclass(frozen=True, slots=True)
class ModeResolution:
    """The effective mode for one scope, and the evidence for it.

    Both observed records are carried, not only the winner, so that an operator looking
    at a banner can see that a tenant is in Safe Mode because of a platform incident
    rather than one of its own -- and so that an audit record of a refusal shows the
    whole precedence input rather than its conclusion.
    """

    mode: OperatingModeName
    scope: ModeScope
    tenant_id: uuid.UUID | None
    decided_by: ModeRecord | None
    global_record: ModeRecord | None
    tenant_record: ModeRecord | None

    @property
    def safe_mode(self) -> bool:
        return self.mode is OperatingModeName.SAFE_MODE

    @property
    def reason_code(self) -> str | None:
        """Why the current mode holds, or None when no record has ever been written."""
        return None if self.decided_by is None else self.decided_by.reason_code

    @property
    def actor(self) -> str | None:
        """Who put this scope in its current mode, or None on an empty history."""
        return None if self.decided_by is None else self.decided_by.actor

    @property
    def since(self) -> datetime | None:
        """Database-clock timestamp of the deciding record, or None on empty history."""
        return None if self.decided_by is None else self.decided_by.changed_at


@dataclass(frozen=True, slots=True)
class SafeModeBanner:
    """The visible banner specification 10.3.2 requires, as structured fields.

    Deliberately not a sentence. The surface renders these fields into the buyer's or
    operator's language; it may not alter them, and no model composes them.
    """

    scope: ModeScope
    tenant_id: uuid.UUID | None
    reason_code: str
    actor: str
    since: datetime
    blocked: tuple[GuardedOperation, ...]
    still_available: tuple[GuardedOperation, ...]


@dataclass(frozen=True, slots=True)
class SafeModeActivation:
    """What one activation did: the record it wrote and the grants it withdrew."""

    record: ModeRecord
    revoked_grant_ids: tuple[uuid.UUID, ...]


# --------------------------------------------------------------------------- reading

# One statement, not two. Under READ COMMITTED each statement takes its own snapshot, so
# reading the global record and the tenant record separately could observe a global
# activation that the tenant read did not -- and precedence computed across two snapshots
# is precedence computed on a state that never existed. DISTINCT ON returns the newest
# row of each scope from a single snapshot.
#
# Ties within a scope are real and are broken *fail-closed*, not by insert order.
#
# ``changed_at`` defaults to ``now()``, the transaction timestamp, so two records written
# for one scope in one transaction share it exactly. Insert order cannot be recovered from
# the ids: ``commerce_domain.uuid7`` is a 48-bit millisecond timestamp followed by ten
# random bytes, with no intra-millisecond counter, so two ids minted microseconds apart
# sort in random order roughly half the time. ``id DESC`` therefore only makes the answer
# deterministic per row set; it does not mean "the later insert wins", and relying on it to
# mean that would leave a thrown kill switch reading as ``NORMAL`` on a coin flip.
#
# So ``(mode = 'SAFE_MODE') DESC`` decides first: when several records of one scope share
# the newest ``changed_at``, Safe Mode wins. The cost is that a stand-down written in the
# same transaction as an entry does not take effect and must be issued in its own
# transaction; the benefit is that the ambiguity can never re-open delegated debits. This
# is the same asymmetry :func:`_coerce_mode` applies to an unreadable mode value.
_NEWEST_GLOBAL_ONLY: Final = text(
    """
    SELECT DISTINCT ON (tenant_id)
           id, tenant_id, mode, reason_code, actor, changed_at
      FROM platform_operating_modes
     WHERE tenant_id IS NULL
     ORDER BY tenant_id NULLS LAST, changed_at DESC, (mode = 'SAFE_MODE') DESC, id DESC
    """
)

_NEWEST_PER_SCOPE: Final = text(
    """
    SELECT DISTINCT ON (tenant_id)
           id, tenant_id, mode, reason_code, actor, changed_at
      FROM platform_operating_modes
     WHERE tenant_id IS NULL OR tenant_id = CAST(:tenant AS uuid)
     ORDER BY tenant_id NULLS LAST, changed_at DESC, (mode = 'SAFE_MODE') DESC, id DESC
    """
)


def _coerce_mode(raw: str) -> OperatingModeName:
    """Read a stored mode string, treating anything unrecognized as ``SAFE_MODE``.

    The check constraint on ``platform_operating_modes`` makes an unrecognized value
    unreachable today. It is handled anyway because the failure is asymmetric: reading a
    corrupt or future mode value as ``NORMAL`` would silently re-open delegated debits
    during an incident, while reading it as ``SAFE_MODE`` costs an operator one
    investigation. A kill switch whose position cannot be read is assumed to be on.
    """
    return (
        OperatingModeName.NORMAL if raw == OperatingModeName.NORMAL else OperatingModeName.SAFE_MODE
    )


def _view(row: Any) -> ModeRecord:
    """Map one history row onto a :class:`ModeRecord`."""
    return ModeRecord(
        record_id=row.id,
        tenant_id=row.tenant_id,
        mode=_coerce_mode(row.mode),
        reason_code=row.reason_code,
        actor=row.actor,
        changed_at=row.changed_at,
    )


def _effective_mode(
    tenant: uuid.UUID | None,
    tenant_record: ModeRecord | None,
    global_record: ModeRecord | None,
) -> ModeResolution:
    """Apply scope precedence to the newest record of each scope.

    The rule, restated from the module docstring because this is where it is enforced:
    the tenant's own record decides, *except* that a global ``SAFE_MODE`` declared
    strictly later than the tenant's last record covers the tenant anyway. Escalation
    propagates downward; de-escalation does not. An empty history is ``NORMAL``.
    """
    if tenant_record is None:
        if global_record is None:
            # Nothing has ever been declared. A platform that has not had an incident is
            # not in one; failing closed here would refuse every delegated payment on a
            # freshly migrated database.
            return ModeResolution(
                mode=OperatingModeName.NORMAL,
                scope=ModeScope.GLOBAL,
                tenant_id=tenant,
                decided_by=None,
                global_record=None,
                tenant_record=None,
            )
        return ModeResolution(
            mode=global_record.mode,
            scope=ModeScope.GLOBAL,
            tenant_id=tenant,
            decided_by=global_record,
            global_record=global_record,
            tenant_record=None,
        )

    # A global SAFE_MODE that lands after the tenant's last decision overrides it. Without
    # this clause a per-tenant NORMAL written months ago would exempt that tenant from
    # every future platform-wide incident, silently, for as long as nobody noticed.
    if (
        global_record is not None
        and global_record.mode is OperatingModeName.SAFE_MODE
        and global_record.changed_at > tenant_record.changed_at
    ):
        return ModeResolution(
            mode=OperatingModeName.SAFE_MODE,
            scope=ModeScope.GLOBAL,
            tenant_id=tenant,
            decided_by=global_record,
            global_record=global_record,
            tenant_record=tenant_record,
        )

    # Everything else is the tenant's own decision, including a tenant SAFE_MODE that a
    # later global NORMAL does not lift: a platform stand-down is not evidence that this
    # tenant's own incident is over.
    return ModeResolution(
        mode=tenant_record.mode,
        scope=ModeScope.TENANT,
        tenant_id=tenant,
        decided_by=tenant_record,
        global_record=global_record,
        tenant_record=tenant_record,
    )


def _guard_scope(session: Session, tenant: uuid.UUID | None) -> None:
    """Refuse a mode question or change that crosses the transaction's tenant boundary.

    ``platform_operating_modes`` has a nullable tenant and so is outside row-level
    security. RLS is what stops cross-tenant reads everywhere else in this kernel; here
    there is nothing but this check, so it is not defence in depth, it is the defence.
    A transaction bound to tenant A may ask about tenant A or about the global scope, and
    nothing else.
    """
    bound = current_tenant(session)
    if bound is not None and tenant is not None and bound != tenant:
        raise SafeModeScopeError(
            f"transaction is bound to tenant {bound}; it may not read or change the "
            f"operating mode of tenant {tenant}"
        )


def resolve_mode(session: Session, tenant: uuid.UUID | None = None) -> ModeResolution:
    """The effective operating mode for ``tenant``, with the records that decided it.

    Guarantees:

    * The newest record of each scope is read in a single statement, so precedence is
      computed over one consistent snapshot even under READ COMMITTED.
    * Precedence is exactly the rule in :func:`_resolve`: the tenant's own record decides,
      unless a global ``SAFE_MODE`` was declared strictly later than it.
    * An empty history resolves to ``NORMAL``; an unrecognized stored mode resolves to
      ``SAFE_MODE``.

    Refuses, with :class:`SafeModeScopeError`, to answer for a tenant other than the one
    bound to this transaction. Pass ``tenant=None`` for the platform-wide scope.
    """
    _guard_scope(session, tenant)

    # Two constant statements rather than one with a nullable bind: a NULL uuid parameter
    # leaves PostgreSQL unable to infer the placeholder's type on some drivers, and the
    # branch is clearer than the cast that would be needed to work around it.
    if tenant is None:
        rows = session.execute(_NEWEST_GLOBAL_ONLY).all()
    else:
        rows = session.execute(_NEWEST_PER_SCOPE, {"tenant": tenant}).all()

    tenant_record: ModeRecord | None = None
    global_record: ModeRecord | None = None
    for row in rows:
        record = _view(row)
        if record.tenant_id is None:
            global_record = record
        else:
            tenant_record = record
    return _effective_mode(tenant, tenant_record, global_record)


def current_mode(session: Session, tenant: uuid.UUID | None = None) -> OperatingModeName:
    """The effective mode for ``tenant``. Shorthand for ``resolve_mode(...).mode``."""
    return resolve_mode(session, tenant).mode


def _permission_for(
    resolution: ModeResolution, operation: GuardedOperation
) -> tuple[bool, RecoveryCode]:
    """Apply the Safe Mode classification to one already-resolved mode.

    Split out from :func:`is_permitted` so that :func:`assert_permitted` can answer and
    build its error from a single resolution. Re-resolving to build the error message
    would let a mode change committed between the two reads produce a refusal whose
    attached evidence contradicts the refusal itself.
    """
    if resolution.mode is OperatingModeName.NORMAL:
        return True, RecoveryCode.OK
    if operation in SAFE_MODE_PERMITTED:
        return True, RecoveryCode.OK
    return False, RecoveryCode.SAFE_MODE_ACTIVE


def _checked_operation(operation: GuardedOperation) -> GuardedOperation:
    """Refuse anything that is not a classified operation.

    :class:`GuardedOperation` is a ``StrEnum``, so a bare ``"READ"`` would compare and
    hash equal to :attr:`GuardedOperation.READ` and slip through the allowlist by
    coincidence. Requiring the enum means a caller that invents an operation name is
    told, rather than silently granted whatever the coincidence produced.
    """
    if not isinstance(operation, GuardedOperation):
        raise ValueError(
            f"operation must be a GuardedOperation, got {type(operation).__name__} "
            f"{operation!r}; Safe Mode does not accept unclassified operation names"
        )
    return operation


def is_permitted(
    session: Session, tenant: uuid.UUID | None, operation: GuardedOperation
) -> tuple[bool, RecoveryCode]:
    """Whether Safe Mode allows ``operation`` for ``tenant`` right now.

    Returns ``(True, RecoveryCode.OK)`` or ``(False, RecoveryCode.SAFE_MODE_ACTIVE)``.

    Guarantees:

    * Under ``NORMAL`` every guarded operation is permitted -- this gate, and only this
      gate, is open. A ``True`` here is **not** an authorization: reservation freshness,
      approval binding, authority epoch, capability and idempotency are separate gates
      and every one of them still applies.
    * Under ``SAFE_MODE`` the answer is membership of :data:`SAFE_MODE_PERMITTED`, an
      allowlist. An operation nobody classified is refused rather than admitted.
    * The mode is re-read from PostgreSQL inside the caller's transaction on every call,
      so a Safe Mode activation that commits mid-flight is observed by the next admission
      rather than by a cache refresh some seconds later.

    Refuses a bare string with :class:`ValueError`; see :func:`_checked_operation`.
    """
    checked = _checked_operation(operation)
    return _permission_for(resolve_mode(session, tenant), checked)


def assert_permitted(
    session: Session, tenant: uuid.UUID | None, operation: GuardedOperation
) -> None:
    """Fail-closed form of :func:`is_permitted` for call sites that must not continue.

    Raises :class:`SafeModeBlockedError`, carrying ``RecoveryCode.SAFE_MODE_ACTIVE`` and
    the :class:`ModeResolution` that produced it, so a caller that forgets to inspect a
    returned boolean stops instead of proceeding. Returns None when permitted.
    """
    checked = _checked_operation(operation)
    resolution = resolve_mode(session, tenant)
    permitted, _code = _permission_for(resolution, checked)
    if not permitted:
        raise SafeModeBlockedError(
            f"{checked.value} is blocked: {resolution.scope.value} scope is in "
            f"SAFE_MODE since {resolution.since} ({resolution.reason_code})",
            resolution=resolution,
        )


def banner(resolution: ModeResolution) -> SafeModeBanner | None:
    """The operator- and buyer-visible banner for a resolution, or None under ``NORMAL``.

    Specification 10.3.2 requires activation to produce a visible banner carrying the
    reason code and actor. Returns structured fields only; rendering is the surface's job
    and no model composes the text.

    Returns None when the scope is not in Safe Mode, and also when a Safe Mode resolution
    somehow has no deciding record -- which cannot happen, since the only path to
    ``SAFE_MODE`` is a stored record -- rather than fabricating a reason or an actor for a
    banner. An unattributed kill-switch banner is worse than none.
    """
    if not resolution.safe_mode or resolution.decided_by is None:
        return None
    decided = resolution.decided_by
    return SafeModeBanner(
        scope=resolution.scope,
        tenant_id=resolution.tenant_id,
        reason_code=decided.reason_code,
        actor=decided.actor,
        since=decided.changed_at,
        blocked=tuple(sorted(SAFE_MODE_BLOCKED)),
        still_available=tuple(sorted(SAFE_MODE_PERMITTED)),
    )


# --------------------------------------------------------------------------- writing


def _validate_actor(actor: str, actor_type: ActorType) -> str:
    """Check the attribution a mode change must carry, or raise.

    Refuses :attr:`~transaction_kernel.contracts.ActorType.AGENT` because specification
    10.3.2 states the LLM cannot enter or leave Safe Mode. This is a structural rule, not
    authentication: proving the operator is who they claim to be happens above this
    module, and this check would not survive a caller that simply lied about the actor
    type. What it does prevent is the ordinary accident -- an agent-facing tool wired
    straight through to the kill switch.

    Refuses a blank actor because a mode change nobody is attributable for is not the
    audited administrative action the specification requires; it is an anonymous edit to
    the platform's payment posture.
    """
    if not isinstance(actor_type, ActorType):
        raise ValueError(f"actor_type must be an ActorType, got {type(actor_type).__name__}")
    if actor_type is ActorType.AGENT:
        raise ValueError(
            "an AGENT may not enter or leave Safe Mode; specification 10.3.2 reserves "
            "the switch for an operator or an allowlisted deterministic incident rule"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor is required; an unattributed mode change is not auditable")
    return actor.strip()


def _write_mode(
    session: Session,
    *,
    tenant: uuid.UUID | None,
    mode: OperatingModeName,
    reason: ModeChangeReason,
    actor: str,
) -> ModeRecord:
    """Append one mode record. Never updates or deletes; history is the audit trail.

    ``changed_at`` is left to the column's ``now()`` server default. An
    application-supplied timestamp would let a pod with a skewed clock write a record that
    sorts before an earlier one, and scope precedence is decided by comparing exactly
    these timestamps -- a backdated global activation would silently lose to the tenant
    record it was meant to override.
    """
    row: OperatingMode = session.scalars(
        insert(OperatingMode)
        .values(
            id=uuid7(),
            tenant_id=tenant,
            mode=mode.value,
            reason_code=reason.value,
            actor=actor,
        )
        .returning(OperatingMode)
    ).one()
    return ModeRecord(
        record_id=row.id,
        tenant_id=row.tenant_id,
        mode=_coerce_mode(row.mode),
        reason_code=row.reason_code,
        actor=row.actor,
        changed_at=row.changed_at,
    )


def _bind_scope_for_write(session: Session, tenant: uuid.UUID | None) -> None:
    """Require the transaction's tenant binding to match the scope being changed.

    A global change requires that *no* tenant is bound. This is the rule that stops a
    tenant-scoped request path from reaching the platform-wide switch by passing
    ``tenant=None``: the table is outside RLS, so without this a single mis-wired handler
    would let one merchant's traffic stop delegated payments for everybody.

    A per-tenant change requires that tenant to be bound, which additionally guarantees
    the activation sweep in :func:`enter_safe_mode` can actually see that tenant's grants
    -- an activation that wrote the record but silently swept nothing would look like it
    had worked.
    """
    if tenant is None:
        bound = current_tenant(session)
        if bound is not None:
            raise SafeModeScopeError(
                f"a global mode change must be made in a transaction with no tenant bound; "
                f"this one is bound to {bound}"
            )
        return
    bound = require_tenant(session)
    if bound != tenant:
        raise SafeModeScopeError(
            f"a mode change for tenant {tenant} must run in a transaction bound to that "
            f"tenant; this one is bound to {bound}"
        )


def _checked_sweep(revoke_operations: Collection[Operation] | None) -> Collection[Operation]:
    """The operations the activation sweep may withdraw, or raise.

    Called before the mode record is written, so a call asking for a protected operation
    leaves no history behind: a refused activation must not look like one that happened.
    """
    if revoke_operations is None:
        return DELEGATED_GRANT_OPERATIONS
    protected = sorted(NEVER_SWEPT.intersection(revoke_operations))
    if protected:
        raise ValueError(
            f"the Safe Mode sweep may not withdraw {protected}; revoking a live refund "
            f"grant would cancel money the buyer is already owed, which is the one thing "
            f"a kill switch thrown to protect that buyer must not do"
        )
    return revoke_operations


def enter_safe_mode(
    session: Session,
    *,
    tenant: uuid.UUID | None,
    reason: ModeChangeReason,
    actor: str,
    actor_type: ActorType = ActorType.OPERATOR,
    revoke_operations: Collection[Operation] | None = None,
) -> SafeModeActivation:
    """Enter Safe Mode for one scope and withdraw that scope's unused delegated grants.

    Guarantees:

    * A record is appended to ``platform_operating_modes`` carrying the mode, the closed
      reason code, the actor and a ``changed_at`` written by the database clock. Nothing
      is updated and nothing is deleted, so the previous mode and its actor remain
      readable forever.
    * For a per-tenant activation, every ``ISSUED`` grant of that tenant whose operation
      is in ``revoke_operations`` (default :data:`DELEGATED_GRANT_OPERATIONS`) is moved to
      ``REVOKED`` in the same transaction. ``CONSUMED`` grants are untouched: Safe Mode
      stops new movement, it does not rewrite the record of movement already attempted.
    * Refund grants are never swept, so a refund already admitted still completes. This
      holds against the ``revoke_operations`` override too, not only against its default:
      naming :data:`~transaction_kernel.contracts.Operation.REFUND_EXECUTE` is refused.
    * Payment attempts are not touched at all. An ``UNKNOWN`` outcome stays ``UNKNOWN``
      and stays reconcilable; an incident is not evidence that a charge failed.

    Refuses:

    * ``ActorType.AGENT`` or a blank actor (:class:`ValueError`) -- specification 10.3.2
      reserves the switch for an operator or an allowlisted incident rule.
    * A reason outside :data:`ENTRY_REASONS` (:class:`ValueError`).
    * A ``revoke_operations`` naming anything in :data:`NEVER_SWEPT` (:class:`ValueError`),
      checked before the record is written so a refused activation leaves no history.
    * A scope that does not match the transaction's tenant binding
      (:class:`SafeModeScopeError`).

    Note on a global activation: no grant sweep runs, because a transaction with no tenant
    bound cannot see any tenant's grants under RLS. ``revoked_grant_ids`` is empty and the
    per-tenant sweep is follow-up work. This is safe, and it is why the sweep is cleanup
    rather than enforcement: an unswept grant is still refused, because admission asks
    :func:`is_permitted` before it consumes anything.

    Re-entering while already in Safe Mode is not an error. It appends a fresh record --
    two operators declaring the same incident is real history worth keeping -- and runs
    the sweep again, which is idempotent because there is nothing left to revoke.
    """
    if reason not in ENTRY_REASONS:
        raise ValueError(
            f"{reason!r} is not an entry reason; entering Safe Mode must name why "
            f"delegated money movement stopped, one of {sorted(ENTRY_REASONS)}"
        )
    attributed = _validate_actor(actor, actor_type)
    operations = _checked_sweep(revoke_operations)
    _bind_scope_for_write(session, tenant)

    record = _write_mode(
        session,
        tenant=tenant,
        mode=OperatingModeName.SAFE_MODE,
        reason=reason,
        actor=attributed,
    )

    revoked: tuple[uuid.UUID, ...] = ()
    if tenant is not None:
        revoked = revoke_unused_grants(session, operations=operations)
    return SafeModeActivation(record=record, revoked_grant_ids=revoked)


def leave_safe_mode(
    session: Session,
    *,
    tenant: uuid.UUID | None,
    reason: ModeChangeReason,
    actor: str,
    actor_type: ActorType = ActorType.OPERATOR,
) -> ModeRecord:
    """Return one scope to ``NORMAL``. An authenticated, audited administrative action.

    Guarantees:

    * A ``NORMAL`` record is appended with the exit reason, the actor and a database-clock
      timestamp. The Safe Mode record it supersedes stays in the history unchanged.
    * Grants revoked on activation are **not** resurrected. A revoked grant is dead; a
      delegated debit that was stopped must be re-admitted, which re-checks the
      reservation, the approval and the authority epoch from scratch. Reviving a grant
      minted before an incident would mean executing an authorization that was issued
      under conditions the incident invalidated.
    * Leaving the global scope does not lift a tenant's own Safe Mode; see the precedence
      rule in :func:`_resolve`.

    Refuses the same actor, reason and scope violations as :func:`enter_safe_mode`, with
    the reason drawn from :data:`EXIT_REASONS`. Calling it while already ``NORMAL`` is not
    an error: it appends a record saying so, which is a truthful piece of history.
    """
    if reason not in EXIT_REASONS:
        raise ValueError(
            f"{reason!r} is not an exit reason; returning to NORMAL must name why it is "
            f"safe to resume, one of {sorted(EXIT_REASONS)}"
        )
    attributed = _validate_actor(actor, actor_type)
    _bind_scope_for_write(session, tenant)
    return _write_mode(
        session,
        tenant=tenant,
        mode=OperatingModeName.NORMAL,
        reason=reason,
        actor=attributed,
    )
