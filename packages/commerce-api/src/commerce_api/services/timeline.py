"""The action timeline of specification 26.1, merged from committed rows and nothing else.

What this module is for
-----------------------
A judge watching the demonstration asks one question of every screen: *how do you know?*
The timeline is the answer. It is the ordered story of one checkout -- who asked for what,
what the merchant said at the time, which version and hash the buyer approved, what the
kernel decided and why, which Execution Grant authorized which provider call, and what
Razorpay reported back.

Every row here is a projection of something already committed: an ``audit_events`` row, a
``webhook_inbox`` row, an ``outbox_events`` row or a ``scenario_runs`` row. Nothing is
computed from memory, nothing is inferred, and nothing is written. A timeline that could
disagree with the ledger would be a narration rather than evidence.

Five streams, and why each one is here
--------------------------------------
====================  ======================================================================
``checkout``          The kernel's own stream for this checkout id: version created,
                      approval required, approval recorded, admission allowed or denied,
                      provider request recorded, evidence applied. The payments module
                      audits onto this stream too, keyed by the attempt's ``checkout_id``,
                      so the money story and the consent story stay in one order.
``payment_attempt``   The per-attempt stream: ``grant.linked`` and the refund lifecycle.
                      Separate in the database because a refund is an operation on an
                      attempt, not on a checkout version.
``outbox_command``    The durable commands this checkout produced. The gap between "the
                      kernel admitted" and "Razorpay answered" is a queue, and a timeline
                      that hides the queue cannot explain a delay.
``webhook``           What the provider actually delivered, including redeliveries marked
                      duplicate. Step 14 of the primary scenario is exactly this row
                      saying "duplicate, state unchanged".
``merchant``          Merchant state changes during this checkout's life. Two things
                      write these rows and only one of them is apparatus: the scenario
                      controller stages a change to demonstrate revalidation, and an
                      approved merchant action carries out a change a person actually
                      agreed to. Both land as ``SCENARIO_INJECTION``, because executing
                      an action reuses the injection machinery, so ``scenario_injection``
                      is decided per row rather than by the stream (ADR 0003 D11,
                      specification 31.3): the difference between "we injected this" and
                      "the merchant changed their price" is the credibility of the whole
                      demonstration.
====================  ======================================================================

Ordering and the cursor
-----------------------
Rows are ordered by ``(occurred_at, source rank, aggregate id, seq)`` and every row carries
that tuple back as an opaque, lexicographically sortable ``cursor``. Several events written
in one transaction share a timestamp -- correctly, they describe one atomic change -- so the
timestamp alone cannot order them; ``seq`` within a stream is the authority, and the rest of
the tuple only breaks ties between streams deterministically.

The cursor exists so ``GET /v1/checkouts/{id}/events`` can resume. Specification 24.2:
"reconnect resumes from event ID, not from in-memory assumptions". :func:`read_after`
re-reads the streams from PostgreSQL and drops everything at or before the cursor, so a
resumed stream is correct even when the process that served the first half is gone.

Redaction
---------
Specification 26.1's last line: secrets, full signatures and unnecessary buyer identifiers
are redacted. :func:`redact` blanks any key whose name looks like protocol material,
shortens 64-hex digests to a readable prefix, and abbreviates buyer references. The
kernel's payloads already hold digests rather than bodies; this is the second fence.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import transaction_kernel as tk
from commerce_domain import ActorType
from merchant_sim import SCENARIO_LABEL
from platform_db import Checkout, ExecutionGrant, PaymentAttempt, ScenarioRun
from platform_db.schema_service import MerchantAction as MerchantActionRow
from sqlalchemy import Row, select, text
from sqlalchemy.orm import Session
from transaction_kernel.audit import AuditEventView

__all__ = [
    "HASH_SHORTHAND",
    "MERCHANT_AGGREGATE",
    "SCENARIO_LABEL",
    "OutboxRow",
    "TimelineEntry",
    "TimelineSource",
    "attempts_of",
    "collect",
    "grants_of",
    "head_of",
    "is_terminal",
    "outbox_for",
    "read_after",
    "redact",
    "short_hash",
    "webhooks_of",
]

#: The audit ``aggregate_type`` a merchant-state change is appended under, keyed by
#: ``merchant_id``. Every writer goes through ``scenario_service.apply_injection`` and so
#: writes ``ScenarioInjection.to_audit_payload()``: the scenario controller (build unit F)
#: staging a demo change, and the Merchant Controller carrying out an approved merchant
#: action. This module reads the stream and nothing else does.
MERCHANT_AGGREGATE: Final[str] = "merchant"

#: What ``merchant_action_service`` puts in an injection's ``note`` when an approved
#: merchant action is what drove it, followed by that action's id.
#:
#: The note is the only field that differs between the two writers -- the payload is
#: otherwise ``to_audit_payload()`` byte for byte -- so this prefix is the whole of the
#: coupling, and it is a string rather than a shared constant because the timeline may not
#: import the Controller. That is also why a match is not believed on its own: the note is
#: free text an operator supplies on ``POST /v1/scenario/injections``, so a staged change
#: could carry these words by accident or by design. The id is looked up in
#: ``merchant_actions`` -- and only an action that reached ``EXECUTING`` or ``SUCCEEDED``
#: clears it, because a row exists from the moment somebody drafts one and a draft has
#: moved nothing.
#:
#: What that is worth, stated honestly rather than flatteringly: it closes the accident
#: completely, and it raises the deliberate case from "draft an action, quote its id" to
#: "run a real merchant action through approval and execution, then stage a separate
#: injection quoting it". That is a strange thing to do and leaves its own trail, but it
#: is not a wall, and :func:`_performed_actions` says so at more length.
_MERCHANT_ACTION_NOTE: Final[str] = "merchant action "

#: How much of a 64-character hex digest a timeline row shows. Twelve characters is
#: 48 bits: plenty for a human to match two rows on screen, useless for reconstructing
#: anything. The proof chain shows hashes in full, because verifying is its job.
HASH_SHORTHAND: Final[int] = 12

#: Payload keys whose *values* never leave the process, matched as case-insensitive
#: substrings so ``x_razorpay_signature`` and ``webhook_secret`` are both caught.
_SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "signature",
    "secret",
    "token",
    "password",
    "authorization",
    "raw_body",
    "credential",
    "api_key",
)

#: Payload keys holding a pseudonymous buyer identifier. Kept, but abbreviated: the
#: timeline needs to show that two rows concern one buyer, never who that buyer is.
_BUYER_KEY_MARKERS: Final[tuple[str, ...]] = ("buyer_ref", "buyer_id", "customer")

_REDACTED: Final[str] = "[redacted]"

#: The two shapes a digest takes in this repository. ``commerce_domain.canonical_hash``
#: returns unpadded base64url (43 characters); ``sha256_hex`` returns 64 hex characters.
#: Both are shortened wherever they appear inside a payload, so a full approval hash never
#: reaches a timeline row that was only asked for a shorthand.
_HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")
_HEX_LENGTH: Final[int] = 64
_B64URL: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_B64URL_SHA256_LENGTH: Final[int] = 43


class TimelineSource(StrEnum):
    """Which stream a row came from. The value is part of the cursor, so it is stable."""

    CHECKOUT = "checkout"
    PAYMENT_ATTEMPT = "payment_attempt"
    OUTBOX_COMMAND = "outbox_command"
    WEBHOOK = "webhook"
    MERCHANT = "merchant"


#: Tie-break rank inside one millisecond. Arbitrary but fixed, because an ordering that
#: varied between two reads of the same committed data would make the cursor useless.
_RANK: Final[Mapping[TimelineSource, int]] = {
    TimelineSource.CHECKOUT: 0,
    TimelineSource.PAYMENT_ATTEMPT: 1,
    TimelineSource.OUTBOX_COMMAND: 2,
    TimelineSource.WEBHOOK: 3,
    TimelineSource.MERCHANT: 4,
}

#: Event types that carry a kernel decision. Restricted by name rather than by "has a
#: ``code`` field", because several payment payloads also carry a ``code`` and folding
#: those into a decision block would render a provider outcome as a kernel ruling.
_DECISION_EVENTS: Final[frozenset[str]] = frozenset({"admission.allowed", "admission.denied"})


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One row of specification 26.1's timeline.

    The named fields are that section's list. ``details`` carries whatever else the source
    row held, redacted, so a reviewer is never asked to trust the summary.
    """

    cursor: str
    occurred_at: datetime
    source: TimelineSource
    actor: str
    action: str
    summary: str
    correlation_id: uuid.UUID
    scenario_injection: bool = False

    checkout_version: int | None = None
    content_hash_short: str | None = None
    policy_version: str | None = None
    policy_receipt_hash_short: str | None = None
    freshness: Mapping[str, Any] | None = None
    approval_ref: str | None = None
    authority_epoch: int | None = None
    decision: Mapping[str, Any] | None = None
    grant: Mapping[str, Any] | None = None
    payment_attempt_id: uuid.UUID | None = None
    provider: Mapping[str, Any] | None = None
    reconciliation: Mapping[str, Any] | None = None
    refund: Mapping[str, Any] | None = None
    audit: Mapping[str, Any] | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One ``outbox_events`` row, detached from the transaction that read it."""

    command_id: uuid.UUID
    command_type: str
    payload: Mapping[str, Any]
    status: str
    attempts: int
    correlation_id: uuid.UUID
    created_at: datetime
    available_at: datetime
    leased_until: datetime | None


# ------------------------------------------------------------------------- redaction


def redact(value: object) -> Any:
    """Return ``value`` with secrets blanked, digests shortened and buyer refs abbreviated.

    Recursive over mappings and sequences, and applied to every payload before it reaches
    a response -- including payloads this service did not write, so a future audit
    producer cannot widen what the timeline discloses by accident.
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                out[name] = _REDACTED
            elif any(marker in lowered for marker in _BUYER_KEY_MARKERS) and isinstance(item, str):
                out[name] = _abbreviate(item, 8)
            else:
                out[name] = redact(item)
        return out
    if isinstance(value, str):
        return _shorten_digest(value)
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def _shorten_digest(value: str) -> str:
    """Shorten a bare SHA-256 digest in either encoding; leave every other string alone."""
    if len(value) == _HEX_LENGTH and all(char in _HEX for char in value):
        return value[:HASH_SHORTHAND] + "…"
    if len(value) == _B64URL_SHA256_LENGTH and all(char in _B64URL for char in value):
        return value[:HASH_SHORTHAND] + "…"
    return value


def short_hash(value: str | None) -> str | None:
    """A hash in the shorthand specification 26.1 asks the timeline to display."""
    if value is None:
        return None
    return value[:HASH_SHORTHAND] + "…" if len(value) > HASH_SHORTHAND else value


def _abbreviate(value: str, keep: int) -> str:
    return value[:keep] + "…" if len(value) > keep else value


# --------------------------------------------------------------------------- cursor


def _cursor(occurred_at: datetime, source: TimelineSource, aggregate: uuid.UUID, seq: int) -> str:
    """A sortable opaque position.

    Formatted so lexicographic comparison of two cursors is exactly tuple comparison of
    ``(epoch_us, rank, aggregate, seq)``. That is what lets :func:`read_after` resume with
    a string comparison and no second ordering rule to keep in step with this one.

    Microseconds, not milliseconds. Audit events are stamped by the kernel truncated to
    milliseconds, but ``outbox_events`` and ``webhook_inbox`` carry PostgreSQL's full
    ``now()``; truncating here would let a row written 800 microseconds later sort ahead
    of one written first, and a timeline whose order disagrees with its own timestamps is
    not evidence of anything.
    """
    epoch_us = int(occurred_at.timestamp() * 1_000_000)
    return f"{epoch_us:018d}.{_RANK[source]}.{aggregate.hex}.{seq:06d}"


# ---------------------------------------------------------------- payload extraction
#
# Kernel payloads are JSON primitives read back from JSONB, so every value arrives as
# ``Any``. These helpers narrow before a value reaches a typed field; without them a
# malformed payload would travel into the response as whatever it happened to be.


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _integer(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _present(mapping: Mapping[str, Any]) -> dict[str, Any] | None:
    """Drop a nested block entirely when every field in it is absent."""
    kept = {key: item for key, item in mapping.items() if item is not None}
    return kept or None


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _claimed_action(payload: Mapping[str, Any]) -> uuid.UUID | None:
    """The merchant action an injection payload says drove it, if it says one at all.

    A claim, not a finding: the note is free text and is checked against
    ``merchant_actions`` by :func:`_performed_actions` before anything is decided on it.
    """
    note = _text(payload, "note")
    if note is None or not note.startswith(_MERCHANT_ACTION_NOTE):
        return None
    return _as_uuid(note[len(_MERCHANT_ACTION_NOTE) :].strip())


# -------------------------------------------------------------------------- summaries

#: One deterministic sentence per known event type, built from that event's own committed
#: fields. Deterministic on purpose: the explanation a buyer or a judge reads is never
#: written by a model, so it can never describe something that did not happen.
_SUMMARIES: Final[Mapping[str, str]] = {
    "checkout.version_created": "Checkout version {version} created and quoted.",
    "checkout.approval_required": (
        "Version {version} frozen, reserved and put to the buyer for approval."
    ),
    "checkout.transitioned": "Checkout version {version} moved from {from} to {to}.",
    "checkout.cancelled": "Checkout version {version} cancelled.",
    "checkout.cancel_denied": "Cancellation refused: {explanation}.",
    "checkout.invalidated_open": (
        "Version {version} invalidated while the payment surface was open; "
        "awaiting the payment result."
    ),
    "approval.recorded": "Buyer approved version {version}.",
    "approval.consumed": "The approval for version {version} was consumed by admission.",
    "approval.rejected": (
        "The approval for version {version} was rejected and its reservation released."
    ),
    "approval.expired": "The approval for version {version} expired unused.",
    # A hold is the one approval event that changes no state, which is exactly why the
    # timeline has to say it happened: without this line an auditor sees an approval card
    # sitting unanswered for ten minutes and no record that the buyer was asked at all.
    "approval.held": (
        "The buyer was asked about version {version} and declined for now ({reason}); "
        "nothing was approved, cancelled or paid."
    ),
    "admission.allowed": (
        "Kernel admitted version {version}: one payment attempt and one Execution Grant."
    ),
    "admission.denied": "Kernel denied admission for version {version}: {explanation}.",
    "grant.linked": "Execution Grant carried by durable command {outbox_command_id}.",
    "provider.request_recorded": "Provider {operation} recorded: {outcome_code}.",
    "payment.order_result_recorded": (
        "Create-order outcome {kind}: payment moved {state_before} to {state_after}."
    ),
    "payment.order_recovered": "An existing provider order was recovered by receipt.",
    "payment.browser_callback": (
        "Browser callback recorded as evidence only; a callback is never capture."
    ),
    "payment.evidence_applied": (
        "Provider evidence applied: payment {state_before} to {state_after}."
    ),
    "evidence.mismatch": (
        "Provider evidence did not match this attempt; escalated for human review."
    ),
    "payment.reconciliation_started": "Reconciliation started for an uncertain outcome.",
    "payment.reconciliation_run_recorded": (
        "Reconciliation attempt {attempt_number} of {bound}: {decision}."
    ),
    "human_review.opened": "Human review case opened: {reason_family}.",
    "webhook.applied": "Webhook applied with status {apply_status}.",
    "refund.admitted": "Refund admitted; one Execution Grant issued.",
    "refund.result_recorded": "Refund result recorded: {state_after}.",
    "refund.reconciled": "Refund reconciled against the provider.",
}


class _SafeFields(dict[str, Any]):
    """``format_map`` source rendering an absent or non-scalar field as ``unknown``.

    A summary sits on a payments screen. A ``KeyError`` from a payload that gained or lost
    a field would take the whole timeline down with it, and a raw ``None`` or a nested
    object rendered into a sentence would read to a human as a fact.
    """

    def __missing__(self, key: str) -> str:  # pragma: no cover - __getitem__ never raises
        return "unknown"

    def __getitem__(self, key: str) -> Any:
        if key not in self:
            return "unknown"
        value = super().__getitem__(key)
        if isinstance(value, bool) or not isinstance(value, str | int):
            return "unknown"
        return value


def _summarise(event_type: str, payload: Mapping[str, Any]) -> str:
    """A sentence for one event, built from its own committed fields."""
    template = _SUMMARIES.get(event_type)
    if template is None:
        return event_type.replace(".", " ").replace("_", " ").capitalize() + "."
    try:
        return template.format_map(_SafeFields(payload))
    except ValueError, IndexError:  # pragma: no cover - guards a malformed template
        return event_type


# ---------------------------------------------------------------------- audit -> entry


def _from_audit(event: AuditEventView, source: TimelineSource) -> TimelineEntry:
    """Project one audit event into a timeline row.

    Mapped by field name across every kernel payload rather than per event type: the
    kernel names ``version``, ``content_hash``, ``grant_id`` and the rest consistently, so
    a per-type mapping would need editing every time the kernel gains an event. The
    decision and refund blocks are the exceptions, keyed on event type because their field
    names are ambiguous across payloads.
    """
    payload = event.payload
    version = _integer(payload, "version")
    if version is None:
        version = _integer(payload, "checkout_version")

    decision: dict[str, Any] | None = None
    if event.event_type in _DECISION_EVENTS:
        decision = {
            "decision_id": _text(payload, "decision_id"),
            "allowed": event.event_type == "admission.allowed",
            "code": _text(payload, "code"),
            "explanation": _text(payload, "explanation"),
            "next_version": _integer(payload, "next_version"),
        }

    grant: dict[str, Any] | None = None
    if _text(payload, "grant_id") or _text(payload, "outbox_command_id"):
        grant = _present(
            {
                "grant_id": _text(payload, "grant_id"),
                "outbox_command_id": _text(payload, "outbox_command_id"),
                "operation": _text(payload, "operation"),
            }
        )

    refund: dict[str, Any] | None = None
    if "refund" in event.event_type:
        refund = _present(
            {
                "refund_id": _text(payload, "refund_id"),
                "state": _text(payload, "state_after"),
                "amount_refunded_minor": _integer(payload, "amount_refunded_minor"),
            }
        )

    return TimelineEntry(
        cursor=_cursor(event.occurred_at, source, event.aggregate_id, event.seq),
        occurred_at=event.occurred_at,
        source=source,
        actor=event.actor_type,
        action=event.event_type,
        summary=_summarise(event.event_type, payload),
        correlation_id=event.correlation_id,
        # The label says an injection wrote this row; it does not say who asked for one.
        # An approved merchant action writes the same label, and :func:`_merchant_entries`
        # is where that is checked, because the check needs the database and this does not.
        scenario_injection=_text(payload, "label") == SCENARIO_LABEL,
        checkout_version=version,
        content_hash_short=short_hash(_text(payload, "content_hash")),
        policy_version=_text(payload, "policy_version"),
        policy_receipt_hash_short=short_hash(
            _text(payload, "policy_receipt_hash") or _text(payload, "receipt_hash")
        ),
        freshness=_present(
            {
                "source_id": _text(payload, "source_id"),
                "catalogue_revision": _integer(payload, "catalogue_revision"),
            }
        ),
        approval_ref=_text(payload, "approval_id"),
        authority_epoch=_integer(payload, "authority_epoch"),
        decision=decision,
        grant=grant,
        payment_attempt_id=_as_uuid(_text(payload, "payment_attempt_id")),
        provider=_present(
            {
                "provider_order_id": _text(payload, "provider_order_id"),
                "provider_payment_id": _text(payload, "provider_payment_id"),
                "http_status": _integer(payload, "http_status"),
                "outcome_code": _text(payload, "outcome_code"),
                "provider_error_code": _text(payload, "provider_error_code"),
                "state_after": _text(payload, "state_after"),
            }
        ),
        reconciliation=_present(
            {
                "reconciliation_run_id": _text(payload, "reconciliation_run_id"),
                "attempt_number": _integer(payload, "attempt_number"),
                "bound": _integer(payload, "bound"),
                "decision": _text(payload, "decision"),
                "case_key": _text(payload, "case_key"),
            }
        ),
        refund=refund,
        audit={
            "event_id": str(event.event_id),
            "seq": event.seq,
            "self_hash": short_hash(event.self_hash),
            "aggregate_type": event.aggregate_type,
            "aggregate_id": str(event.aggregate_id),
        },
        details=redact(payload),
    )


# ------------------------------------------------------------------------ row readers
#
# The two statements below are composed from literals in this module. Nothing from a
# request or a row is joined into SQL text; every variable travels as a bound parameter.
# The array casts matter: an empty Python list has no inferable element type, and without
# the cast PostgreSQL refuses the statement instead of matching nothing.

_SELECT_WEBHOOKS = text(
    "SELECT id, dedup_key, provider_event_id, event_type, body_digest, signature_verified,"
    "       payment_id, order_id, refund_id, received_at, applied_at, apply_status,"
    "       apply_reason, state_before, state_after, changed, duplicate_count,"
    "       outbox_command_id"
    " FROM webhook_inbox"
    " WHERE tenant_id = :t"
    "   AND (order_id = ANY(CAST(:orders AS text[]))"
    "        OR payment_id = ANY(CAST(:payments AS text[])))"
    " ORDER BY received_at, id"
)

_SELECT_OUTBOX = text(
    "SELECT id, command_type, payload, status, attempts, correlation_id, created_at,"
    "       available_at, leased_until"
    " FROM outbox_events"
    " WHERE tenant_id = :t"
    "   AND (payload->>'checkout_id' = :c"
    "        OR payload->>'payment_attempt_id' = ANY(CAST(:attempts AS text[]))"
    "        OR payload->>'inbox_id' = ANY(CAST(:inboxes AS text[])))"
    " ORDER BY created_at, id"
)


def attempts_of(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> tuple[PaymentAttempt, ...]:
    """Every payment attempt on this checkout, oldest first."""
    rows = session.execute(
        select(PaymentAttempt)
        .where(PaymentAttempt.tenant_id == tenant_id, PaymentAttempt.checkout_id == checkout_id)
        .order_by(PaymentAttempt.created_at, PaymentAttempt.id)
    ).scalars()
    return tuple(rows)


def webhooks_of(
    session: Session, *, tenant_id: uuid.UUID, attempts: Sequence[PaymentAttempt]
) -> tuple[Row[Any], ...]:
    """Inbox rows naming one of this checkout's provider identifiers.

    Matched on the identifiers rather than on a correlation id: a webhook is delivered by
    Razorpay and carries no notion of the request that started this, so the provider order
    and payment ids are the only honest join.
    """
    orders = [a.provider_order_id for a in attempts if a.provider_order_id]
    payments = [a.provider_payment_id for a in attempts if a.provider_payment_id]
    if not orders and not payments:
        return ()
    rows = session.execute(
        _SELECT_WEBHOOKS, {"t": tenant_id, "orders": orders, "payments": payments}
    ).all()
    return tuple(rows)


def outbox_for(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    attempts: Sequence[PaymentAttempt],
    inbox_ids: Sequence[uuid.UUID],
) -> tuple[OutboxRow, ...]:
    """Durable commands belonging to this checkout.

    Selected by what the payload names -- ``checkout_id``, ``payment_attempt_id`` or
    ``inbox_id`` -- rather than by correlation id. A correlation id is a log-joining
    convenience that anything may set; the payload fields are the command's own subject.
    """
    rows = session.execute(
        _SELECT_OUTBOX,
        {
            "t": tenant_id,
            "c": str(checkout_id),
            "attempts": [str(a.id) for a in attempts],
            "inboxes": [str(i) for i in inbox_ids],
        },
    ).all()
    return tuple(
        OutboxRow(
            command_id=row.id,
            command_type=str(row.command_type),
            payload=dict(row.payload),
            status=str(row.status),
            attempts=int(row.attempts),
            correlation_id=row.correlation_id,
            created_at=row.created_at,
            available_at=row.available_at,
            leased_until=row.leased_until,
        )
        for row in rows
    )


def grants_of(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> tuple[ExecutionGrant, ...]:
    """Every Execution Grant issued against this checkout, oldest first."""
    rows = session.execute(
        select(ExecutionGrant)
        .where(ExecutionGrant.tenant_id == tenant_id, ExecutionGrant.checkout_id == checkout_id)
        .order_by(ExecutionGrant.issued_at, ExecutionGrant.id)
    ).scalars()
    return tuple(rows)


def head_of(session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID) -> Checkout | None:
    """The ``checkouts`` head row, for callers deciding access from ``buyer_ref``."""
    return session.execute(
        select(Checkout).where(Checkout.tenant_id == tenant_id, Checkout.id == checkout_id)
    ).scalar_one_or_none()


def is_terminal(session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID) -> bool:
    """True when this checkout can produce no further checkout-level events.

    Used by the SSE route to end a stream rather than hold a connection open forever on a
    journey that has finished. ``PAID``, ``INVALIDATED``, ``CANCELLED`` and ``EXPIRED``
    are the kernel's terminal set.
    """
    head = tk.read_head(session, tenant_id=tenant_id, checkout_id=checkout_id)
    return head is not None and head.status in tk.TERMINAL_CHECKOUT_STATES


# ------------------------------------------------------------------------ collection


def collect(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> tuple[TimelineEntry, ...]:
    """Every timeline row for one checkout, in order.

    Reads the whole of each stream. Deliberate, and bounded by what these aggregates hold:
    a checkout accumulates tens of events, not millions, and a hash chain cannot be
    verified from a page anyway. An aggregate that ever grew unbounded would need a
    checkpointing scheme here, not a ``LIMIT``.

    Runs on the app role inside the caller's read transaction. Writes nothing.
    """
    head = tk.read_head(session, tenant_id=tenant_id, checkout_id=checkout_id)
    if head is None:
        return ()

    entries: list[TimelineEntry] = [
        _from_audit(event, TimelineSource.CHECKOUT)
        for event in tk.read_stream(
            session, tenant=tenant_id, aggregate_type="checkout", aggregate_id=checkout_id
        )
    ]

    attempts = attempts_of(session, tenant_id=tenant_id, checkout_id=checkout_id)
    for attempt in attempts:
        entries.extend(
            _from_audit(event, TimelineSource.PAYMENT_ATTEMPT)
            for event in tk.read_stream(
                session,
                tenant=tenant_id,
                aggregate_type="payment_attempt",
                aggregate_id=attempt.id,
            )
        )

    inbox_rows = webhooks_of(session, tenant_id=tenant_id, attempts=attempts)
    entries.extend(_webhook_entries(session, tenant_id, inbox_rows))
    entries.extend(
        _outbox_entries(
            outbox_for(
                session,
                tenant_id=tenant_id,
                checkout_id=checkout_id,
                attempts=attempts,
                inbox_ids=[row.id for row in inbox_rows],
            )
        )
    )
    entries.extend(_merchant_entries(session, tenant_id, head))

    entries.sort(key=lambda entry: entry.cursor)
    return tuple(entries)


def read_after(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID, cursor: str | None
) -> tuple[TimelineEntry, ...]:
    """Rows strictly after ``cursor``, re-read from the database.

    Specification 24.2: a reconnecting client resumes from an event id, not from anything
    the process remembers. The cursor is compared as a string, which is the comparison
    :func:`_cursor` was formatted to make equal to tuple ordering, so a stream resumed
    against a different pod returns exactly the rows the first one had not sent.

    An unparseable or unknown cursor is not an error: it selects everything after it,
    which for a malformed value is the whole timeline. Refusing would strand a client
    whose ``Last-Event-ID`` was corrupted with no way to catch up.
    """
    entries = collect(session, tenant_id=tenant_id, checkout_id=checkout_id)
    if cursor is None:
        return entries
    return tuple(entry for entry in entries if entry.cursor > cursor)


def _webhook_entries(
    session: Session, tenant_id: uuid.UUID, inbox_rows: Sequence[Row[Any]]
) -> list[TimelineEntry]:
    """A row for each delivery, plus that inbox row's own audit stream.

    ``duplicate_count`` is on the delivery row because a redelivery does not create a
    second row -- the receiver's ``ON CONFLICT DO NOTHING`` increments this instead. That
    is exactly what step 14 of the primary scenario shows, so it is surfaced by name
    rather than left inside ``details``.
    """
    entries: list[TimelineEntry] = []
    for row in inbox_rows:
        duplicates = int(row.duplicate_count)
        tail = (
            f"seen {duplicates} time(s) before, state unchanged."
            if duplicates
            else f"stored for durable apply ({row.apply_status})."
        )
        entries.append(
            TimelineEntry(
                cursor=_cursor(row.received_at, TimelineSource.WEBHOOK, row.id, 0),
                occurred_at=row.received_at,
                source=TimelineSource.WEBHOOK,
                # Not an ActorType: this row records what the provider sent, and
                # attributing it to SYSTEM would hide where the fact came from.
                actor="RAZORPAY",
                action=f"webhook.received:{row.event_type}",
                summary=f"Razorpay delivered {row.event_type}; {tail}",
                # A delivery has no correlation column of its own; the inbox row id is
                # the thread every command about this event is joined on.
                correlation_id=row.id,
                provider=_present(
                    {
                        "provider_order_id": row.order_id,
                        "provider_payment_id": row.payment_id,
                        "provider_refund_id": row.refund_id,
                        "state_after": row.state_after,
                    }
                ),
                details=redact(
                    {
                        "inbox_id": str(row.id),
                        "event_type": row.event_type,
                        "provider_event_id": row.provider_event_id,
                        "signature_verified": row.signature_verified,
                        "apply_status": row.apply_status,
                        "apply_reason": row.apply_reason,
                        "duplicate_count": duplicates,
                        "body_digest": row.body_digest,
                        "changed": row.changed,
                    }
                ),
            )
        )
        entries.extend(
            _from_audit(event, TimelineSource.WEBHOOK)
            for event in tk.read_stream(
                session, tenant=tenant_id, aggregate_type="webhook_inbox", aggregate_id=row.id
            )
        )
    return entries


def _outbox_entries(rows: Sequence[OutboxRow]) -> list[TimelineEntry]:
    return [
        TimelineEntry(
            cursor=_cursor(row.created_at, TimelineSource.OUTBOX_COMMAND, row.command_id, 0),
            occurred_at=row.created_at,
            source=TimelineSource.OUTBOX_COMMAND,
            actor=str(ActorType.SYSTEM),
            action=f"outbox.enqueued:{row.command_type}",
            summary=(
                f"Durable command {row.command_type} enqueued; "
                f"status {row.status} after {row.attempts} attempt(s)."
            ),
            correlation_id=row.correlation_id,
            payment_attempt_id=_as_uuid(_text(row.payload, "payment_attempt_id")),
            grant=_present({"grant_id": _text(row.payload, "grant_id")}),
            details=redact(
                {
                    "outbox_command_id": str(row.command_id),
                    "command_type": row.command_type,
                    "status": row.status,
                    "attempts": row.attempts,
                    "payload": row.payload,
                }
            ),
        )
        for row in rows
    ]


#: The action states that mean a change was actually carried out.
#:
#: ``EXECUTING`` is included with ``SUCCEEDED`` because the injection and the state move
#: happen in one transaction: the row is set EXECUTING, the shop is changed, and the row
#: becomes SUCCEEDED. A timeline read that lands between those two writes is reading a
#: change that has genuinely happened, and calling it staged for the width of one
#: transaction would be its own wrong answer.
_CARRIED_OUT: Final[tuple[str, ...]] = ("EXECUTING", "SUCCEEDED")


def _performed_actions(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    claimed: set[uuid.UUID],
) -> frozenset[uuid.UUID]:
    """Which of the claimed action ids belong to an action this merchant actually ran.

    Queried rather than trusted, because the note a row carries is free text and costs
    nothing to write. Scoped by merchant as well as tenant, so a note naming a sibling
    merchant's action -- the only other id that would exist -- clears nothing.

    **And scoped by state, which is the part that took a second reading.** A
    ``merchant_actions`` row exists from the moment somebody drafts one:
    ``POST /v1/merchant/actions`` inserts it as ``DRAFT`` before any submit, approval or
    execution. A lookup on id alone therefore answered "somebody with a merchant session
    created this id", not "this action ran" -- and a draft is exactly what an operator
    staging an injection could obtain, with the same scenario key, in one request. Only
    ``EXECUTING`` and ``SUCCEEDED`` mean a change was actually carried out; a draft, a
    rejection or a stale approval moved nothing and must not launder a staged event.

    What this does and does not promise. It closes the accidental case completely: prose
    like "merchant action needed here" does not parse as a UUID, and a UUID that names
    nothing matches no row. It raises the deliberate case from "draft an action and quote
    its id" to "carry a real merchant action through approval and execution, then stage a
    separate injection quoting it" -- which is a considerably stranger thing to do and
    leaves its own audit trail, but is not impossible. Saying so plainly is better than a
    comment that claims a wall where there is a fence.

    Skipped entirely when nothing claimed an action, which is every timeline with no
    merchant-side change on it.
    """
    if not claimed:
        return frozenset()
    rows = session.execute(
        select(MerchantActionRow.id).where(
            MerchantActionRow.tenant_id == tenant_id,
            MerchantActionRow.merchant_id == merchant_id,
            MerchantActionRow.id.in_(claimed),
            MerchantActionRow.state.in_(_CARRIED_OUT),
        )
    ).scalars()
    return frozenset(rows)


def _merchant_entries(
    session: Session, tenant_id: uuid.UUID, head: tk.CheckoutHead
) -> list[TimelineEntry]:
    """Merchant-state changes during this checkout's life, staged ones told from real ones.

    Bounded to events at or after the checkout was created. The merchant stream is not
    checkout-scoped -- one merchant serves every buyer -- so an unbounded read would put
    another shopper's price change into this timeline, which is worse than useless during
    a demonstration of *this* checkout's revalidation.

    ``scenario_runs`` rows are folded in for injections whose audit event is not on the
    merchant stream, so a demo change is never silently absent; an injection present in
    both is shown once, from the chained audit row, which is the stronger evidence.

    ``scenario_injection`` is settled here, on the row, and not by the fact that the row is
    a ``SCENARIO_INJECTION``. Carrying out an approved merchant action calls
    ``scenario_service.apply_injection``, so a price the merchant genuinely changed writes
    the same payload the demo lever writes; reading the label alone would report that
    change to a reviewer as staged, which is the opposite of what the field is for. The
    ``note`` names the action, ``merchant_actions`` confirms it exists, and only a
    confirmed one is demoted -- an unmatched claim stays labelled as apparatus, because
    the failure that matters is a staged change passing as organic, not the reverse.
    """
    events = tk.read_stream(
        session,
        tenant=tenant_id,
        aggregate_type=MERCHANT_AGGREGATE,
        aggregate_id=head.merchant_id,
    )
    in_window = [event for event in events if event.occurred_at >= head.created_at]
    audited = {
        _text(event.payload, "injection_id")
        for event in events
        if _text(event.payload, "injection_id") is not None
    }
    runs = [
        row
        for row in session.execute(
            select(ScenarioRun)
            .where(
                ScenarioRun.tenant_id == tenant_id,
                ScenarioRun.merchant_id == head.merchant_id,
                ScenarioRun.created_at >= head.created_at,
            )
            .order_by(ScenarioRun.created_at, ScenarioRun.id)
        ).scalars()
        if str(row.injection_id) not in audited
    ]

    payloads: list[Mapping[str, Any]] = [event.payload for event in in_window]
    payloads.extend(row.payload for row in runs)
    performed = _performed_actions(
        session,
        tenant_id=tenant_id,
        merchant_id=head.merchant_id,
        claimed={
            action_id for payload in payloads if (action_id := _claimed_action(payload)) is not None
        },
    )

    entries: list[TimelineEntry] = []
    for event in in_window:
        entry = _from_audit(event, TimelineSource.MERCHANT)
        action_id = _claimed_action(event.payload)
        if action_id is not None and action_id in performed:
            entry = replace(
                entry,
                scenario_injection=False,
                summary=(
                    f"Merchant action {action_id} changed merchant state: "
                    f"{_text(event.payload, 'kind') or 'unknown'}."
                ),
            )
        entries.append(entry)

    for row in runs:
        action_id = _claimed_action(row.payload)
        performed_here = action_id is not None and action_id in performed
        entries.append(
            TimelineEntry(
                cursor=_cursor(row.created_at, TimelineSource.MERCHANT, row.id, 0),
                occurred_at=row.created_at,
                source=TimelineSource.MERCHANT,
                actor=str(ActorType.OPERATOR),
                # The action name stays ``merchant.injection`` for both writers because it
                # names the shape of the row -- a change to merchant state -- and a client
                # matching on it wants every such change. Which kind of change it was is
                # ``scenario_injection``, and that is the field to read.
                action=f"merchant.injection:{row.kind}",
                summary=(
                    f"Merchant action {action_id} changed merchant state: {row.kind}."
                    if performed_here
                    else f"Scenario injection {row.kind} changed merchant state."
                ),
                correlation_id=row.injection_id,
                scenario_injection=not performed_here,
                details=redact({"injection_id": str(row.injection_id), "payload": row.payload}),
            )
        )
    return entries
