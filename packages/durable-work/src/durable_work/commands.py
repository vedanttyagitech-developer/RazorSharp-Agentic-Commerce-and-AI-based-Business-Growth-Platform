"""Outbox command vocabulary shared by the API process and the worker.

The outbox stores a ``command_type`` string and a JSON ``payload``. Nothing in
:mod:`durable_work.outbox` says what those mean; this module does. The API enqueues one of
the commands below, the worker parses the row back into the same frozen dataclass, and
every handler is written against that dataclass rather than against a loosely-typed dict.

Three rules shape everything here.

*Only canonicalizable primitives.* A payload is validated at enqueue by RFC 8785
canonicalization in the integer-only profile, which refuses floats. Every field is
therefore a ``str``, an ``int`` or a mapping of ``str`` to ``str``: UUIDs travel as their
canonical hyphenated text, money as integer minor units beside a three-letter currency
code, timestamps (should a command ever carry one) as RFC 3339 strings. A ``uuid.UUID``
or ``datetime`` object never appears on a command, so ``to_payload()`` is a plain field
copy and the stored bytes are byte-for-byte reproducible from the dataclass.

*Strict on the way back in.* :meth:`from_payload` refuses a missing key, an unknown key,
a wrongly typed value and a version other than :data:`COMMAND_VERSION`. A payload written
by a newer or older release, or hand-edited in the table, fails loudly in the worker
rather than being executed with a field silently defaulted. Failing here aborts nothing
financial: the command has not been acted on yet.

*The grant binding comes from the payload.* :meth:`CreateOrderCommand.grant_binding` and
:meth:`RefundExecuteCommand.grant_binding` build the exact
:class:`~transaction_kernel.grants.GrantBinding` that
:func:`~transaction_kernel.grants.consume_grant` compares against the admitted grant.
They are built from the command the worker holds and never from the grant row. That is
the whole point of the check: the worker proves it is about to send the same tenant,
checkout, version, content hash, attempt, operation, amount and currency the kernel
issued the grant for. A binding derived from the grant row would always match and the
verification would be theatre.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Final, Self

from commerce_domain import CheckoutRef, Money, MoneyError
from platform_db import require_tenant
from sqlalchemy.orm import Session
from transaction_kernel.contracts import Operation
from transaction_kernel.grants import GrantBinding

from .outbox import LeasedCommand, OutboxCommand, OutboxUsageError, enqueue

__all__ = [
    "COMMAND_VERSION",
    "IDEMPOTENCY_KEY_FIELD",
    "VERSION_FIELD",
    "AnyCommand",
    "ApplyWebhookEventCommand",
    "CommandType",
    "CreateOrderCommand",
    "ReserveDebitCommand",
    "ReserveReconcileCommand",
    "ReconcilePaymentCommand",
    "ReconcileRefundCommand",
    "RefundExecuteCommand",
    "enqueue_command",
    "idempotency_key_of",
    "parse_command",
    "parse_leased_command",
]


class CommandType(StrEnum):
    """Every ``command_type`` the worker dispatches on.

    The two grant-bearing types carry the same names as the kernel's
    :class:`~transaction_kernel.contracts.Operation` values on purpose: a reviewer reading
    an ``outbox_events`` row beside an ``execution_grants`` row should see the same word.
    """

    RESERVE_DEBIT = "RESERVE_DEBIT"
    RESERVE_RECONCILE = "RESERVE_RECONCILE"
    PAYMENT_CREATE_ORDER = "PAYMENT_CREATE_ORDER"
    APPLY_WEBHOOK_EVENT = "APPLY_WEBHOOK_EVENT"
    RECONCILE_PAYMENT = "RECONCILE_PAYMENT"
    REFUND_EXECUTE = "REFUND_EXECUTE"
    RECONCILE_REFUND = "RECONCILE_REFUND"


#: Payload schema version. Bumped when a field is added, removed or changes meaning; the
#: worker refuses any other value so a rolling deploy can never execute a command whose
#: shape it does not understand.
COMMAND_VERSION: Final = 1

#: Envelope key carrying :data:`COMMAND_VERSION`. Short because it is in every row.
VERSION_FIELD: Final = "v"

#: Optional envelope key: the HTTP ``Idempotency-Key`` of the request that enqueued the
#: command (ADR D9). Evidence for the timeline, never an input to a handler, which is why
#: it lives in the envelope and not on the dataclasses.
IDEMPOTENCY_KEY_FIELD: Final = "idempotency_key"

#: Razorpay stores at most 15 ``notes`` keys of at most 256 characters each. Checked at
#: construction so the limit is hit in the API's transaction, not as a 400 from the
#: provider after the grant has already been consumed.
_MAX_NOTES: Final = 15
_MAX_NOTE_LENGTH: Final = 256

#: Notes the create-order command must carry, and must carry consistently with its own
#: fields: they are what a Razorpay dashboard shows a reviewer beside the order, and a
#: note that disagrees with the command it rode on is evidence pointing the wrong way.
_REQUIRED_NOTES: Final = ("tenant_id", "checkout_id", "payment_attempt_id")

#: Upper bound on any free-text identifier in a payload. Wide enough for every provider
#: id and receipt in use; narrow enough that a payload cannot smuggle a document.
_MAX_TEXT: Final = 512


# ------------------------------------------------------------------ field validation
#
# Each helper raises OutboxUsageError with the field name in the message. They are the
# single place where "wrong type" is decided, so that the dataclasses' __post_init__ and
# from_payload cannot drift apart: from_payload only extracts, __post_init__ validates.


def _reject_bool(value: object, name: str) -> None:
    # bool is an int subclass; an amount of True would canonicalize as 1 and pass.
    if isinstance(value, bool):
        raise OutboxUsageError(f"{name} must not be a boolean")


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise OutboxUsageError(f"{name} must be str, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise OutboxUsageError(f"{name} must not be blank")
    if len(value) > _MAX_TEXT:
        raise OutboxUsageError(f"{name} exceeds {_MAX_TEXT} characters")
    return value


def _uuid_text(value: object, name: str) -> str:
    """A UUID in its one canonical text form.

    Parseable is not enough: ``consume_grant`` compares ``uuid.UUID`` objects, so any
    parseable form would bind correctly, but the stored bytes are also hashed into the
    proof chain, and ``{...}``, upper-case and un-hyphenated spellings of one id would
    give three different hashes for one command.
    """
    text = _text(value, name)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise OutboxUsageError(f"{name} must be a UUID string, got {text!r}") from exc
    if str(parsed) != text:
        raise OutboxUsageError(f"{name} must be a canonical lower-case hyphenated UUID")
    return text


def _positive_int(value: object, name: str) -> int:
    _reject_bool(value, name)
    if not isinstance(value, int):
        raise OutboxUsageError(f"{name} must be int, got {type(value).__name__}")
    if value < 1:
        raise OutboxUsageError(f"{name} must be at least 1, got {value}")
    return value


def _money(amount_minor: object, currency: object) -> Money:
    """Validate an amount exactly as the kernel will read it back.

    ``Money`` is the arbiter: it refuses bools, floats, lower-case or unsupported
    currencies. Using it here means a command can only carry an amount the grant binding
    can be built from, and a zero amount is refused because no grant is ever issued for
    one.
    """
    _reject_bool(amount_minor, "amount_minor")
    if not isinstance(amount_minor, int):
        raise OutboxUsageError(f"amount_minor must be int, got {type(amount_minor).__name__}")
    if not isinstance(currency, str):
        raise OutboxUsageError(f"currency must be str, got {type(currency).__name__}")
    try:
        money = Money(amount_minor, currency)
    except MoneyError as exc:
        raise OutboxUsageError(f"amount is not valid money: {exc}") from exc
    if money.minor <= 0:
        raise OutboxUsageError(f"amount_minor must be positive, got {money.minor}")
    return money


def _notes(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise OutboxUsageError(f"notes must be a mapping, got {type(value).__name__}")
    if len(value) > _MAX_NOTES:
        raise OutboxUsageError(f"notes may carry at most {_MAX_NOTES} keys, got {len(value)}")
    frozen: dict[str, str] = {}
    for key, note in value.items():
        if not isinstance(key, str) or not key:
            raise OutboxUsageError(f"notes keys must be non-empty str, got {key!r}")
        if not isinstance(note, str):
            raise OutboxUsageError(f"notes[{key!r}] must be str, got {type(note).__name__}")
        if len(note) > _MAX_NOTE_LENGTH:
            raise OutboxUsageError(f"notes[{key!r}] exceeds {_MAX_NOTE_LENGTH} characters")
        frozen[key] = note
    return MappingProxyType(frozen)


# ------------------------------------------------------------------- envelope + base


def _check_envelope(payload: Mapping[str, object], expected: frozenset[str], name: str) -> None:
    """Refuse a payload whose key set or version is not exactly what ``name`` expects.

    Missing and unknown keys are reported together, by name, because an operator reading
    a dead-lettered command needs the whole shape of the mismatch, not the first key the
    dict happened to iterate.
    """
    if not isinstance(payload, Mapping):
        raise OutboxUsageError(f"{name} payload must be a mapping, got {type(payload).__name__}")
    version = payload.get(VERSION_FIELD)
    if VERSION_FIELD not in payload:
        raise OutboxUsageError(f"{name} payload has no {VERSION_FIELD!r} version field")
    if isinstance(version, bool) or not isinstance(version, int) or version != COMMAND_VERSION:
        raise OutboxUsageError(
            f"{name} payload is version {version!r}; this worker understands {COMMAND_VERSION}"
        )
    keys = frozenset(payload) - {VERSION_FIELD, IDEMPOTENCY_KEY_FIELD}
    missing = sorted(expected - keys)
    unknown = sorted(keys - expected)
    if missing or unknown:
        raise OutboxUsageError(
            f"{name} payload keys do not match: missing={missing} unknown={unknown}"
        )
    if IDEMPOTENCY_KEY_FIELD in payload:
        _text(payload[IDEMPOTENCY_KEY_FIELD], IDEMPOTENCY_KEY_FIELD)


@dataclass(frozen=True, slots=True)
class _Command:
    """What every command shares. Not a public type; use :data:`AnyCommand`.

    ``from_payload`` is generic over the dataclass fields so that adding a field to a
    subclass changes exactly one place. Validation lives in each subclass's
    ``__post_init__`` and runs on every construction path, so a command built directly
    by the API is held to the same rules as one parsed back by the worker.
    """

    command_type: ClassVar[CommandType]

    def to_payload(self) -> dict[str, Any]:
        """The JSON object stored in ``outbox_events.payload``.

        Every value is already a canonicalizable primitive, so this is a copy, not a
        conversion; notes are materialized as a plain dict for the JSON encoder.
        """
        payload: dict[str, Any] = {VERSION_FIELD: COMMAND_VERSION}
        for field in fields(self):
            value = getattr(self, field.name)
            payload[field.name] = dict(value) if isinstance(value, Mapping) else value
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Self:
        """Rebuild the command from a stored payload, refusing anything unexpected."""
        names = frozenset(field.name for field in fields(cls))
        _check_envelope(payload, names, cls.__name__)
        return cls(**{name: payload[name] for name in names})


# ------------------------------------------------------------------------ commands


@dataclass(frozen=True, slots=True)
class CreateOrderCommand(_Command):
    """Create the Razorpay order for one admitted payment attempt.

    Carries everything :func:`~transaction_kernel.grants.consume_grant` compares, so the
    worker never reads the grant row to learn what it is allowed to send. ``content_hash``
    is on the command for exactly that reason: the grant binds the canonical bytes the
    buyer approved, and a command that cannot name them cannot prove it is the admitted
    one.
    """

    command_type: ClassVar[CommandType] = CommandType.PAYMENT_CREATE_ORDER

    tenant_id: str
    payment_attempt_id: str
    grant_id: str
    checkout_id: str
    checkout_version: int
    content_hash: str
    amount_minor: int
    currency: str
    receipt: str
    notes: Mapping[str, str]
    correlation_id: str

    def __post_init__(self) -> None:
        _uuid_text(self.tenant_id, "tenant_id")
        _uuid_text(self.payment_attempt_id, "payment_attempt_id")
        _uuid_text(self.grant_id, "grant_id")
        _uuid_text(self.checkout_id, "checkout_id")
        _positive_int(self.checkout_version, "checkout_version")
        _text(self.content_hash, "content_hash")
        _money(self.amount_minor, self.currency)
        # Razorpay caps receipt at 40 characters; the kernel mints 29. Checked here so a
        # hand-built command cannot be enqueued and then refused by the provider after
        # its grant was consumed.
        if len(_text(self.receipt, "receipt")) > 40:
            raise OutboxUsageError("receipt exceeds Razorpay's 40-character limit")
        _uuid_text(self.correlation_id, "correlation_id")
        notes = _notes(self.notes)
        for key in _REQUIRED_NOTES:
            if notes.get(key) != getattr(self, key):
                raise OutboxUsageError(f"notes[{key!r}] must equal the command's {key}")
        object.__setattr__(self, "notes", notes)

    def grant_binding(self) -> GrantBinding:
        """The binding ``consume_grant`` must accept for this command, from the payload."""
        return GrantBinding(
            tenant_id=uuid.UUID(self.tenant_id),
            checkout=CheckoutRef(
                checkout_id=uuid.UUID(self.checkout_id),
                version=self.checkout_version,
                content_hash=self.content_hash,
            ),
            payment_attempt_id=uuid.UUID(self.payment_attempt_id),
            operation=Operation.PAYMENT_CREATE_ORDER,
            amount=Money(self.amount_minor, self.currency),
        )


@dataclass(frozen=True, slots=True)
class ReserveDebitCommand(CreateOrderCommand):
    """Execute a simulated Reserve debit; never dispatch to Razorpay Checkout."""

    command_type: ClassVar[CommandType] = CommandType.RESERVE_DEBIT

    def grant_binding(self) -> GrantBinding:
        from dataclasses import replace

        return replace(
            super(ReserveDebitCommand, self).grant_binding(), operation=Operation.RESERVE_DEBIT
        )


@dataclass(frozen=True, slots=True)
class ReserveReconcileCommand(ReserveDebitCommand):
    """Read an accepted simulator debit; bounded rounds never send a second debit."""

    command_type: ClassVar[CommandType] = CommandType.RESERVE_RECONCILE
    round: int = 1

    def __post_init__(self) -> None:
        super(ReserveReconcileCommand, self).__post_init__()
        _positive_int(self.round, "round")
        if self.round > 6:
            raise OutboxUsageError("Reserve reconciliation is bounded to six rounds")


@dataclass(frozen=True, slots=True)
class ApplyWebhookEventCommand(_Command):
    """Apply one verified webhook stored in the inbox (ADR D7).

    Only the inbox id travels: the raw body stays in ``webhook_inbox`` and is re-read
    under the kernel role, so a payload cannot carry a different event than the one whose
    signature was verified.
    """

    command_type: ClassVar[CommandType] = CommandType.APPLY_WEBHOOK_EVENT

    tenant_id: str
    inbox_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        _uuid_text(self.tenant_id, "tenant_id")
        _uuid_text(self.inbox_id, "inbox_id")
        _uuid_text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class ReconcilePaymentCommand(_Command):
    """Fetch provider truth for an attempt whose outcome is uncertain (ADR D8, D13).

    ``attempt_number`` is the reconciliation round, counted from 1, so the bound of six
    is enforced on the command itself rather than on outbox retries, which are for
    transport failures and not for "the provider has not decided yet".
    """

    command_type: ClassVar[CommandType] = CommandType.RECONCILE_PAYMENT

    tenant_id: str
    payment_attempt_id: str
    reason: str
    attempt_number: int
    correlation_id: str

    def __post_init__(self) -> None:
        _uuid_text(self.tenant_id, "tenant_id")
        _uuid_text(self.payment_attempt_id, "payment_attempt_id")
        _text(self.reason, "reason")
        _positive_int(self.attempt_number, "attempt_number")
        _uuid_text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class RefundExecuteCommand(_Command):
    """Execute one admitted refund against the provider.

    The refund grant is issued on the payment attempt's checkout reference, so the
    binding needs ``checkout_id``, ``checkout_version`` and ``content_hash`` beside the
    refund amount. ``idem_key`` is the ``X-Razorpay-Idempotency-Key`` chosen at
    admission and stored on the refunds row; carrying it here means a redelivered
    command sends the same key and the provider collapses the duplicate.
    """

    command_type: ClassVar[CommandType] = CommandType.REFUND_EXECUTE

    tenant_id: str
    refund_id: str
    payment_attempt_id: str
    grant_id: str
    checkout_id: str
    checkout_version: int
    content_hash: str
    amount_minor: int
    currency: str
    idem_key: str
    correlation_id: str

    def __post_init__(self) -> None:
        _uuid_text(self.tenant_id, "tenant_id")
        _uuid_text(self.refund_id, "refund_id")
        _uuid_text(self.payment_attempt_id, "payment_attempt_id")
        _uuid_text(self.grant_id, "grant_id")
        _uuid_text(self.checkout_id, "checkout_id")
        _positive_int(self.checkout_version, "checkout_version")
        _text(self.content_hash, "content_hash")
        _money(self.amount_minor, self.currency)
        _text(self.idem_key, "idem_key")
        _uuid_text(self.correlation_id, "correlation_id")

    def grant_binding(self) -> GrantBinding:
        """The binding ``consume_grant`` must accept for this refund, from the payload.

        ``refund_id`` is bound too (ADR D10): the kernel issues a refund grant per
        ``refunds`` row, so a command naming a different refund than the grant was issued
        for is a substitution and is refused, and the consumed grant of one partial refund
        never blocks the admission of the next.
        """
        return GrantBinding(
            tenant_id=uuid.UUID(self.tenant_id),
            checkout=CheckoutRef(
                checkout_id=uuid.UUID(self.checkout_id),
                version=self.checkout_version,
                content_hash=self.content_hash,
            ),
            payment_attempt_id=uuid.UUID(self.payment_attempt_id),
            operation=Operation.REFUND_EXECUTE,
            amount=Money(self.amount_minor, self.currency),
            refund_id=uuid.UUID(self.refund_id),
        )


@dataclass(frozen=True, slots=True)
class ReconcileRefundCommand(_Command):
    """Fetch provider truth for a refund whose outcome is uncertain."""

    command_type: ClassVar[CommandType] = CommandType.RECONCILE_REFUND

    tenant_id: str
    refund_id: str
    payment_attempt_id: str
    reason: str
    attempt_number: int
    correlation_id: str

    def __post_init__(self) -> None:
        _uuid_text(self.tenant_id, "tenant_id")
        _uuid_text(self.refund_id, "refund_id")
        _uuid_text(self.payment_attempt_id, "payment_attempt_id")
        _text(self.reason, "reason")
        _positive_int(self.attempt_number, "attempt_number")
        _uuid_text(self.correlation_id, "correlation_id")


AnyCommand = (
    CreateOrderCommand
    | ReserveDebitCommand
    | ReserveReconcileCommand
    | ApplyWebhookEventCommand
    | ReconcilePaymentCommand
    | RefundExecuteCommand
    | ReconcileRefundCommand
)

_BY_TYPE: Final[Mapping[CommandType, type[AnyCommand]]] = MappingProxyType(
    {
        CommandType.RESERVE_DEBIT: ReserveDebitCommand,
        CommandType.RESERVE_RECONCILE: ReserveReconcileCommand,
        CommandType.PAYMENT_CREATE_ORDER: CreateOrderCommand,
        CommandType.APPLY_WEBHOOK_EVENT: ApplyWebhookEventCommand,
        CommandType.RECONCILE_PAYMENT: ReconcilePaymentCommand,
        CommandType.REFUND_EXECUTE: RefundExecuteCommand,
        CommandType.RECONCILE_REFUND: ReconcileRefundCommand,
    }
)


# ------------------------------------------------------------------------- parsing


def parse_command(command_type: str, payload: Mapping[str, object]) -> AnyCommand:
    """Turn a stored ``(command_type, payload)`` pair into its typed command.

    Refuses an unknown ``command_type`` with :class:`OutboxUsageError` rather than
    returning ``None``: a worker that silently completed a command it did not recognize
    would lose it, and the failure direction for the outbox is "repeat, never skip".
    """
    if not isinstance(command_type, str):
        raise OutboxUsageError(f"command_type must be str, got {type(command_type).__name__}")
    try:
        kind = CommandType(command_type)
    except ValueError as exc:
        raise OutboxUsageError(f"unknown command_type {command_type!r}") from exc
    return _BY_TYPE[kind].from_payload(payload)


def parse_leased_command(leased: LeasedCommand) -> AnyCommand:
    """Parse a leased row and confirm its payload names the row's own tenant.

    The row's ``tenant_id`` is what row-level security scoped the lease by; the payload's
    is what the handler will bind its kernel transaction to. If they ever differ, the
    payload was not written by :func:`enqueue_command`, and acting on it would run one
    tenant's command under another tenant's lease.
    """
    command = parse_command(leased.command_type, leased.payload)
    if uuid.UUID(command.tenant_id) != leased.tenant_id:
        raise OutboxUsageError(
            f"command {leased.command_id} payload names tenant {command.tenant_id}, "
            f"row belongs to {leased.tenant_id}"
        )
    return command


def idempotency_key_of(payload: Mapping[str, object]) -> str | None:
    """The HTTP ``Idempotency-Key`` recorded on the envelope, if the enqueuer had one."""
    value = payload.get(IDEMPOTENCY_KEY_FIELD)
    if value is None:
        return None
    return _text(value, IDEMPOTENCY_KEY_FIELD)


# ------------------------------------------------------------------------ enqueueing


def enqueue_command(
    session: Session,
    command: AnyCommand,
    *,
    idempotency_key: str | None,
    available_in_seconds: int = 0,
) -> OutboxCommand:
    """Enqueue a typed command in the caller's transaction. Never commits.

    Refuses a command whose ``tenant_id`` is not the tenant bound to this transaction.
    :func:`~durable_work.outbox.enqueue` takes the row's tenant from the GUC, never from
    the payload, so without this check a payload naming another tenant would be stored
    under this one's row and later fail in :func:`parse_leased_command` -- correct, but
    hours late and in the worker rather than in the request that made the mistake.
    """
    if not isinstance(command, _Command):
        raise OutboxUsageError(f"command must be an outbox command, got {type(command).__name__}")
    bound = require_tenant(session)
    if uuid.UUID(command.tenant_id) != bound:
        raise OutboxUsageError(
            f"command names tenant {command.tenant_id}, transaction is bound to {bound}"
        )
    payload = command.to_payload()
    if idempotency_key is not None:
        payload[IDEMPOTENCY_KEY_FIELD] = _text(idempotency_key, IDEMPOTENCY_KEY_FIELD)
    return enqueue(
        session,
        command_type=command.command_type.value,
        payload=payload,
        correlation_id=uuid.UUID(command.correlation_id),
        available_in_seconds=available_in_seconds,
    )
